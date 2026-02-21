# src/llm_client.py
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Optional, Tuple, Dict, Union, Iterable

import httpx

from .llm_gateway import get_gateway

logger = logging.getLogger(__name__)

# -----------------------------
# ENV
# -----------------------------
LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "openai").strip().lower()  # openai | deepseek | dummy
LLM_ENABLED = (os.getenv("LLM_ENABLED") or "1").strip() == "1"

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_BASE_URL = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/")
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()

# DeepSeek (API совместим с OpenAI SDK — только base_url и модель другие)
DEEPSEEK_API_KEY = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
DEEPSEEK_BASE_URL = (os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1").strip().rstrip("/")
DEEPSEEK_MODEL = (os.getenv("DEEPSEEK_MODEL") or "deepseek-chat").strip()

# Estimated cost per 1K tokens (input+output avg) for logging
_EST_COST_PER_1K: Dict[str, float] = {
    "gpt-4o": 0.0075,
    "gpt-4o-mini": 0.00030,
    "deepseek-chat": 0.00027,
    "deepseek-reasoner": 0.0014,
}

# Timeouts / retries
# Было 12/8/1 — часто не хватает на большие промпты с oddsBase.
LLM_TOTAL_TIMEOUT_S = float((os.getenv("LLM_TOTAL_TIMEOUT_S") or "45.0").strip())
LLM_ATTEMPT_TIMEOUT_S = float((os.getenv("LLM_ATTEMPT_TIMEOUT_S") or "25.0").strip())
LLM_MAX_RETRIES = int((os.getenv("LLM_MAX_RETRIES") or "2").strip())

OPENAI_TEMPERATURE = float((os.getenv("OPENAI_TEMPERATURE") or "0.1").strip())

# -----------------------------
# Max tokens caps (anti-TPM)
# -----------------------------
# Снижаем токены в LIVE, чтобы не упираться в TPM.
# Можно тюнить через ENV без деплоя кода.
LLM_MAX_TOKENS_LEGACY = int((os.getenv("LLM_MAX_TOKENS_LEGACY") or "900").strip())
LLM_MAX_TOKENS_UI_PRE = int((os.getenv("LLM_MAX_TOKENS_UI_PRE") or "1200").strip())
LLM_MAX_TOKENS_UI_LIVE = int((os.getenv("LLM_MAX_TOKENS_UI_LIVE") or "1500").strip())
LLM_MAX_TOKENS_UI_LIVE_PRO = int((os.getenv("LLM_MAX_TOKENS_UI_LIVE_PRO") or "1200").strip())

# Hard safety cap (на случай неверных ENV)
LLM_MAX_TOKENS_HARD_CAP = int((os.getenv("LLM_MAX_TOKENS_HARD_CAP") or "3000").strip())


# Safety throttles (Telegram-friendly) — лёгкая локальная защита
LLM_PER_USER_MIN_INTERVAL_S = float((os.getenv("LLM_PER_USER_MIN_INTERVAL_S") or "2.5").strip())

LLMOutput = Union["LLMAnalysis", Dict[str, Any]]

_ALLOWED_VERDICTS = {"lean_yes", "lean_no", "unclear"}

_BANNED_PHRASES = (
    "ставь", "ставьте", "бери", "берите", "верняк",
    "гарант", "гарантия", "100%", "фикс"
)
# Removed overly broad words: "лучше" (="better", common sports word),
# "выгодно" (="advantageous"), "проход" (="pass through", common in hockey)

# -----------------------------
# Per-user throttle state (process-local)
# -----------------------------
_PER_USER_LAST_CALL: Dict[int, float] = {}
_PER_USER_LOCK = asyncio.Lock()
# -----------------------------
# LLM Circuit Breaker (quota/429)
# -----------------------------
_LLM_DISABLED_UNTIL_TS: float = 0.0
_LLM_DISABLED_REASON: str = ""


def _llm_is_disabled() -> bool:
    return time.time() < _LLM_DISABLED_UNTIL_TS


def _llm_disable_for(seconds: float, reason: str) -> None:
    global _LLM_DISABLED_UNTIL_TS, _LLM_DISABLED_REASON
    _LLM_DISABLED_UNTIL_TS = time.time() + float(seconds)
    _LLM_DISABLED_REASON = (reason or "")[:240]


def _is_quota_or_429(e: Exception) -> bool:
    s = (str(e) or "").lower()
    return (
        "insufficient_quota" in s
        or "you exceeded your current quota" in s
        or "http 429" in s
        or "status code: 429" in s
        or "rate limit" in s
    )


def _fallback_ui(schema: str, reason: str) -> Dict[str, Any]:
    # Универсальный fallback под UI-схемы
    if schema in {"ui_live", "ui_live_pro"}:
        return _normalize_ui_obj(
            schema,
            {
                "title": "🟢 LIVE-обзор",
                "summary": "AI временно недоступен — показываю базовую справку.",
                "disclaimer": "Аналитический материал, не является рекомендацией.",
                "context": [],
                "markets": [],
                "risks": ["AI временно недоступен — попробуй позже."],
                "debug": {"llm_reason": reason},
            },
        )

    # ui_pre / legacy
    return _normalize_ui_obj(
        "ui_pre",
        {
            "title": "📊 Обзор рынков",
            "summary": "AI временно недоступен — показываю базовую справку.",
            "context": [],
            "insights": [],
            "risks": ["AI временно недоступен — попробуй позже."],
            "disclaimer": "Аналитический материал, не является рекомендацией.",
            "debug": {"llm_reason": reason},
        },
    )



@dataclass
class LLMAnalysis:
    verdict: str                 # "lean_yes" | "lean_no" | "unclear"
    confidence: float            # 0..1
    reasoning_bullets: list[str] # <=5
    risks: list[str]             # <=5
    checklist: list[str]         # <=5

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "reasoning_bullets": self.reasoning_bullets,
            "risks": self.risks,
            "checklist": self.checklist,
        }


# -----------------------------
# Helpers
# -----------------------------
def _find_banned_phrase_in_text(text: str, banned: Iterable[str]) -> Optional[str]:
    t = (text or "").lower()
    for p in banned:
        pp = (p or "").lower().strip()
        if pp and pp in t:
            return p
    return None


def _find_banned_phrase(obj: Any) -> Optional[str]:
    try:
        s = json.dumps(obj, ensure_ascii=False).lower()
    except Exception:
        s = str(obj).lower()
    return _find_banned_phrase_in_text(s, _BANNED_PHRASES)


def _contains_banned_phrases(obj: Any) -> bool:
    return _find_banned_phrase(obj) is not None


def _clamp01(x: Any) -> float:
    try:
        v = float(x)
    except Exception:
        return 0.0
    return 0.0 if v < 0 else 1.0 if v > 1 else v


def _as_str_list(v: Any, max_len: int = 5) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        parts = [p.strip("•- \t") for p in v.splitlines() if p.strip()]
        return parts[:max_len]
    if isinstance(v, list):
        out: list[str] = []
        for item in v:
            if item is None:
                continue
            s = str(item).strip()
            if s:
                out.append(s)
        return out[:max_len]
    return []


def _normalize_ui_obj(schema: str, obj: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(obj or {})

    # Ensure title and disclaimer always present
    if not str(out.get("title") or "").strip():
        out["title"] = "🟢 LIVE-обзор" if "live" in (schema or "") else "📊 Обзор рынков"

    if not str(out.get("disclaimer") or "").strip():
        out["disclaimer"] = "Аналитический материал, не является рекомендацией."

    # Normalize list fields if present but wrong type
    for list_field in ("risks", "context", "key_events", "insights", "pro_angles", "bets", "key_factors", "line_logic"):
        if list_field in out and not isinstance(out[list_field], list):
            out[list_field] = _as_str_list(out.get(list_field), max_len=6)

    return out


def _sleep_backoff(retry_idx: int) -> float:
    """
    Мягкий backoff: 0.35s, 0.75s, 1.4s (+ jitter).
    """
    base = 0.35 * (2 ** retry_idx)
    return base + random.random() * 0.25


def _now() -> float:
    return time.monotonic()


async def _per_user_throttle(user_id: int) -> Optional[float]:
    if LLM_PER_USER_MIN_INTERVAL_S <= 0:
        return None
    async with _PER_USER_LOCK:
        last = _PER_USER_LAST_CALL.get(user_id) or 0.0
        now = _now()
        delta = now - last
        if delta < LLM_PER_USER_MIN_INTERVAL_S:
            return LLM_PER_USER_MIN_INTERVAL_S - delta
        _PER_USER_LAST_CALL[user_id] = now
        return None


def _max_tokens_for_schema(schema: str) -> int:
    """
    Жёстко режем токены в LIVE, чтобы снизить TPM.
    Значения настраиваются через ENV.
    """
    schema = (schema or "").strip()

    if schema == "legacy":
        v = LLM_MAX_TOKENS_LEGACY
    elif schema == "ui_pre":
        v = LLM_MAX_TOKENS_UI_PRE
    elif schema == "ui_live_pro":
        v = LLM_MAX_TOKENS_UI_LIVE_PRO
    else:  # ui_live и любые неизвестные UI-схемы
        v = LLM_MAX_TOKENS_UI_LIVE

    # safety cap
    try:
        v = int(v)
    except Exception:
        v = 220
    if v < 120:
        v = 120
    if v > int(LLM_MAX_TOKENS_HARD_CAP):
        v = int(LLM_MAX_TOKENS_HARD_CAP)
    return v



def _make_repair_domain_prompt(schema: str, original_domain_prompt: str, bad_obj: Any) -> str:
    """
    Просим модель переписать свой JSON, убрав запрещённые фразы.
    Важно: _openai_chat_json сам оборачивает это в user prompt.
    """
    bad_json = ""
    try:
        bad_json = json.dumps(bad_obj, ensure_ascii=False)
    except Exception:
        bad_json = str(bad_obj)

    return (
        "ВНИМАНИЕ: твой предыдущий ответ будет отклонён из-за запрещённых слов/фраз.\n"
        "Нужно переписать ответ, сохранив смысл и СТРУКТУРУ строго по схеме, но без запрещённых слов.\n\n"
        f"Schema: {schema}\n"
        "Запрещённые слова/фразы (нельзя использовать ни в каком виде):\n"
        + ", ".join(_BANNED_PHRASES)
        + "\n\n"
        "Правила:\n"
        "- Верни СТРОГО JSON-объект (без markdown, без текста вне JSON)\n"
        "- НЕ давай прогнозов, советов или призывов\n"
        "- В LIVE не показывай коэффициенты и числа\n\n"
        "Исходный вход (контекст), который нужно учитывать:\n"
        f"{(original_domain_prompt or '').strip()}\n\n"
        "Твой предыдущий JSON (перепиши его корректно):\n"
        f"{bad_json}\n"
    )


# -----------------------------
# Validation
# -----------------------------
def validate_analysis_json(obj: Any) -> Tuple[bool, Optional[LLMAnalysis], str]:
    if not isinstance(obj, dict):
        return False, None, "root is not an object"

    verdict = str(obj.get("verdict", "")).strip()
    if verdict not in _ALLOWED_VERDICTS:
        return False, None, f"bad verdict: {verdict!r}"

    confidence = _clamp01(obj.get("confidence", 0.0))
    reasoning = _as_str_list(obj.get("reasoning_bullets"), max_len=5)
    risks = _as_str_list(obj.get("risks"), max_len=5)
    checklist = _as_str_list(obj.get("checklist"), max_len=5)

    if not reasoning:
        return False, None, "reasoning_bullets is empty"

    return True, LLMAnalysis(
        verdict=verdict,
        confidence=confidence,
        reasoning_bullets=reasoning,
        risks=risks,
        checklist=checklist,
    ), "ok"


def validate_ui_json(schema: str, obj: Any) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    if not isinstance(obj, dict):
        return False, None, "root is not an object"

    title = str(obj.get("title", "")).strip()
    if not title:
        return False, None, "title is empty"

    # Pass through all fields the LLM returned — parsing.py uses its own richer schema
    # (summary, live_state, key_events, insights, pro_angles, bets) which differs from
    # the legacy ui_live schema (context, markets). Accept both by returning obj as-is
    # after normalisation, as long as title exists.
    out = _normalize_ui_obj(schema, obj)
    return True, out, "ok"


# -----------------------------
# Fallbacks
# -----------------------------
def fallback_analysis(_: str) -> LLMAnalysis:
    return LLMAnalysis(
        verdict="unclear",
        confidence=0.35,
        reasoning_bullets=[
            "LLM недоступен/не успел — даю безопасный базовый разбор.",
            "Сверь линию и контекст матча, избегай решений «на эмоциях».",
            "Соблюдай банк-менеджмент: фиксированный % на ставку, без догонов.",
        ],
        risks=[
            "Недостаток входных данных (составы/травмы/мотивация/форма).",
            "Случайность и дисперсия в спорте.",
        ],
        checklist=[
            "Проверь составы/вратарей/ключевых игроков",
            "Проверь календарь (b2b, перелёты)",
            "Сравни линию у 2–3 источников",
            "Определи max stake по банк-менеджменту",
        ],
    )


def _paywall_ui_live(reason: str = "LIVE-анализ доступен в Premium.") -> Dict[str, Any]:
    return {
        "title": "🟢 LIVE-обзор — Premium",
        "context": [
            reason,
            "Premium: больше лимитов и частые обновления по ходу матча.",
            "Открой ⭐ Premium в меню.",
        ],
        "markets": [],
        "risks": [],
        "disclaimer": "Доступ ограничен тарифом.",
    }


# -----------------------------
# Prompts
# -----------------------------
_SYSTEM_PROMPT_LEGACY = """Ты спортивный аналитик.
Отвечай СТРОГО валидным JSON без markdown и без комментариев.
Схема ответа:
{
  "verdict": "lean_yes" | "lean_no" | "unclear",
  "confidence": number (0..1),
  "reasoning_bullets": [string, ...] (1..5),
  "risks": [string, ...] (0..5),
  "checklist": [string, ...] (0..5)
}
Никаких других ключей. Никакого текста вне JSON.
"""

_SYSTEM_PROMPT_UI = """Ты спортивный аналитик.
Отвечай СТРОГО одним JSON-объектом (без markdown, без текста, без пояснений).
ВСЕ ключи обязательны и не пустые.

Запрещено:
- прогнозы, советы, призывы ставить
- слова: ставь, бери, выгодно, лучше, проход, гарантия, 100%

Формат UI:
1) ui_pre:
{
 "title": string,
 "summary": string,
 "key_factors": [string],
 "line_logic": [string],
 "risks": [string],
 "disclaimer": string
}

2) ui_live:
{
 "title": string,
 "context": [string],
 "markets": [{"name": string, "direction":"up|down|flat|unknown", "logic": string}],
 "risks": [string],
 "disclaimer": string
}

Правила:
- disclaimer ОБЯЗАТЕЛЬНО заполнить (короткая фраза на русском).
- В LIVE НЕ показывай коэффициенты и числа — только направление и логика.
- Пиши коротко, списками.
Верни только JSON.
"""


def _make_user_prompt(domain_prompt: str) -> str:
    return (
        "Задача: дай осторожный аналитический разбор, без прямых призывов ставить.\n"
        f"Вход:\n{(domain_prompt or '').strip()}\n"
    )


# -----------------------------
# Provider config resolver
# -----------------------------
def _resolve_provider(provider: str) -> tuple:
    """Return (api_key, base_url, model) for a given provider name."""
    if provider == "deepseek":
        return DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
    return OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL


def _get_fallback_provider() -> Optional[str]:
    """Return fallback provider name, or None if no fallback available."""
    if LLM_PROVIDER == "deepseek" and OPENAI_API_KEY:
        return "openai"
    if LLM_PROVIDER == "openai" and DEEPSEEK_API_KEY:
        return "deepseek"
    return None


# -----------------------------
# LLM API call (supports OpenAI + DeepSeek)
# -----------------------------
async def _llm_chat_json(
    domain_prompt: str,
    timeout_s: float,
    system_prompt: str,
    max_tokens: int,
    provider: Optional[str] = None,
) -> Dict[str, Any]:
    """Call LLM API (OpenAI-compatible). Supports openai and deepseek providers."""
    provider = provider or LLM_PROVIDER
    api_key, base_url, model = _resolve_provider(provider)

    if not api_key:
        raise RuntimeError(f"{provider.upper()}_API_KEY is not set")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    payload: Dict[str, Any] = {
        "model": model,
        "temperature": OPENAI_TEMPERATURE,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _make_user_prompt(domain_prompt)},
        ],
        "max_tokens": int(max_tokens),
    }

    timeout = httpx.Timeout(
        timeout_s,
        connect=min(10.0, timeout_s),
        read=timeout_s,
        write=min(10.0, timeout_s),
        pool=min(10.0, timeout_s),
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise ValueError(f"empty {provider} response content")

    # Log provider, model, tokens usage
    usage = data.get("usage") or {}
    total_tokens = usage.get("total_tokens", 0)
    cost_per_1k = _EST_COST_PER_1K.get(model, 0.0005)
    est_cost = total_tokens / 1000 * cost_per_1k
    logger.info("LLM gateway: provider=%s model=%s tokens=%d est_cost=$%.4f",
                provider, model, total_tokens, est_cost)

    obj = _safe_json_loads(content)
    if not isinstance(obj, dict):
        raise ValueError("model JSON root is not an object")
    return obj


def _safe_json_loads(text: str) -> Any:
    """Parse JSON with repair for truncated/malformed responses from LLM."""
    text = text.strip()
    # Remove markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json) and last line (```)
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines).strip()

    # Try normal parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError as original_err:
        logger.warning("JSON parse failed, attempting repair: %s (first 200 chars: %s)",
                       original_err, text[:200])

    # Repair attempt 1: close unclosed strings and brackets
    repaired = text
    # Count unmatched quotes (ignore escaped ones)
    in_string = False
    for i, ch in enumerate(repaired):
        if ch == '"' and (i == 0 or repaired[i - 1] != '\\'):
            in_string = not in_string
    if in_string:
        repaired += '"'

    # Close unclosed brackets/braces
    stack = []
    in_str = False
    for i, ch in enumerate(repaired):
        if ch == '"' and (i == 0 or repaired[i - 1] != '\\'):
            in_str = not in_str
        if in_str:
            continue
        if ch in ('{', '['):
            stack.append(ch)
        elif ch == '}' and stack and stack[-1] == '{':
            stack.pop()
        elif ch == ']' and stack and stack[-1] == '[':
            stack.pop()

    # Close in reverse order
    for bracket in reversed(stack):
        repaired += '}' if bracket == '{' else ']'

    try:
        obj = json.loads(repaired)
        logger.info("JSON repair SUCCESS (closed %d brackets)", len(stack))
        return obj
    except json.JSONDecodeError:
        pass

    # Repair attempt 2: truncate to last complete key-value pair
    # Find last complete "key": value pattern
    last_brace = repaired.rfind('}')
    if last_brace > 0:
        candidate = repaired[:last_brace + 1]
        # Try closing any remaining open brackets
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # Repair attempt 3: extract JSON object from content
    import re
    match = re.search(r'\{[\s\S]*\}', text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    raise ValueError(f"JSON repair failed, raw text (first 300 chars): {text[:300]}")


# Backward compat alias
async def _openai_chat_json(domain_prompt: str, timeout_s: float, system_prompt: str, max_tokens: int) -> Dict[str, Any]:
    return await _llm_chat_json(domain_prompt, timeout_s, system_prompt, max_tokens)


# -----------------------------
# Main: cached via gateway
# -----------------------------
async def analyze_with_llm_cached(
    domain_prompt: str,
    *,
    cache_key: str,
    ttl_s: Optional[int] = None,
    schema: str = "legacy",
    user_id: Optional[int] = None,
) -> Tuple[LLMOutput, dict]:
    start = time.monotonic()

    if ttl_s is None:
        ttl_s = LLM_CACHE_TTL_LIVE_S if schema == "ui_live" else LLM_CACHE_TTL_S

    # -----------------------------
    # ACCESS CONTROL (free/premium)
    # -----------------------------
    if schema == "ui_live" and user_id is not None:
        try:
            from .user_store import get_user, mark_trial_used  # type: ignore
        except Exception:
            get_user = None
            mark_trial_used = None

        if get_user is not None:
            u = get_user(int(user_id))

            can_live = bool(getattr(u, "can_live", False))
            is_premium = bool(getattr(u, "is_premium", False))
            trial_used = bool(getattr(u, "trial_live_used", True))

            if not can_live:
                meta = {
                    "provider": "access",
                    "attempts": 0,
                    "elapsed_ms": int((time.monotonic() - start) * 1000),
                    "used_fallback": True,
                    "last_error": "premium_required",
                    "cache": "miss",
                }
                return _paywall_ui_live(), meta

            if (not is_premium) and (not trial_used) and (mark_trial_used is not None):
                try:
                    mark_trial_used(int(user_id))
                except Exception:
                    logger.exception("mark_trial_used failed")

    # -----------------------------
    # Disabled
    # -----------------------------
    if not LLM_ENABLED:
        meta = {
            "provider": "disabled",
            "attempts": 0,
            "elapsed_ms": int((time.monotonic() - start) * 1000),
            "used_fallback": True,
            "last_error": "LLM_ENABLED=0",
            "cache": "miss",
        }
        if schema == "legacy":
            return fallback_analysis(domain_prompt), meta
        return _fallback_ui(schema, "AI отключён (LLM_ENABLED=0)."), meta

    # -----------------------------
    # Dummy
    # -----------------------------
    if LLM_PROVIDER == "dummy":
        meta = {
            "provider": "dummy",
            "attempts": 0,
            "elapsed_ms": int((time.monotonic() - start) * 1000),
            "used_fallback": True,
            "last_error": "LLM_PROVIDER=dummy",
            "cache": "miss",
        }
        if schema == "legacy":
            return fallback_analysis(domain_prompt), meta
        return _fallback_ui(schema, "LLM_PROVIDER=dummy — безопасный режим."), meta

    # -----------------------------
    # Telegram-friendly per-user throttle
    # -----------------------------
    if user_id is not None:
        wait_s = await _per_user_throttle(int(user_id))
        if wait_s and wait_s > 0.01:
            meta = {
                "provider": LLM_PROVIDER,
                "attempts": 0,
                "elapsed_ms": int((time.monotonic() - start) * 1000),
                "used_fallback": True,
                "last_error": f"throttle_user:{wait_s:.1f}s",
                "cache": "miss",
            }
            if schema == "legacy":
                return fallback_analysis(domain_prompt), meta
            return _fallback_ui(schema, "Слишком частые запросы — подожди пару секунд."), meta

    kind = "live" if schema == "ui_live" else "pre"
    gw = await get_gateway()

    max_tokens_hint = _max_tokens_for_schema(schema)

    async def _call_llm():
        active_provider = LLM_PROVIDER if LLM_PROVIDER in ("openai", "deepseek") else "openai"
        fallback_provider = _get_fallback_provider()
        deadline = time.monotonic() + max(0.2, float(LLM_TOTAL_TIMEOUT_S))
        attempts = 0
        last_error: Optional[str] = None

        for retry_idx in range(int(LLM_MAX_RETRIES) + 1):
            attempts += 1

            remaining = deadline - time.monotonic()
            if remaining <= 0.15:
                last_error = "deadline_exceeded"
                break

            attempt_timeout = min(float(LLM_ATTEMPT_TIMEOUT_S), max(0.5, remaining))

            try:
                system_prompt = _SYSTEM_PROMPT_LEGACY if schema == "legacy" else _SYSTEM_PROMPT_UI

                obj = await _llm_chat_json(
                    domain_prompt,
                    timeout_s=attempt_timeout,
                    system_prompt=system_prompt,
                    max_tokens=max_tokens_hint,
                    provider=active_provider,
                )

                # ---------- banned-phrases auto-repair (1 retry inside attempt) ----------
                hit = _find_banned_phrase(obj)
                if hit:
                    repair_prompt = _make_repair_domain_prompt(schema, domain_prompt, obj)
                    obj2 = await _llm_chat_json(
                        repair_prompt,
                        timeout_s=attempt_timeout,
                        system_prompt=system_prompt,
                        max_tokens=max_tokens_hint,
                        provider=active_provider,
                    )
                    hit2 = _find_banned_phrase(obj2)
                    if hit2:
                        raise ValueError(f"banned_phrases_in_output:{hit2}")
                    obj = obj2

                if schema == "legacy":
                    ok, analysis, msg = validate_analysis_json(obj)
                    if not ok or analysis is None:
                        raise ValueError(f"schema validation failed: {msg}")
                    return analysis.to_dict(), {
                        "provider": active_provider,
                        "attempts": attempts,
                        "used_fallback": False,
                        "last_error": None,
                    }, {}

                obj2 = _normalize_ui_obj(schema, obj)
                ok, analysis2, msg = validate_ui_json(schema, obj2)
                if not ok or analysis2 is None:
                    raise ValueError(f"ui schema validation failed: {msg}")

                return analysis2, {
                    "provider": active_provider,
                    "attempts": attempts,
                    "used_fallback": False,
                    "last_error": None,
                }, {}

            except httpx.HTTPStatusError as e:
                code = e.response.status_code if e.response is not None else 0
                headers = dict(e.response.headers) if e.response is not None else {}
                last_error = f"http_{code}"

                if code == 429 and e.response is not None:
                    try:
                        body = e.response.text or ""
                    except Exception:
                        body = ""
                    h = {k.lower(): v for k, v in (e.response.headers or {}).items()}
                    logger.warning(
                        "%s HTTP 429. headers=%s body=%s",
                        active_provider,
                        {
                            "retry-after": h.get("retry-after"),
                            "x-request-id": h.get("x-request-id"),
                            "x-ratelimit-remaining-requests": h.get("x-ratelimit-remaining-requests"),
                            "x-ratelimit-remaining-tokens": h.get("x-ratelimit-remaining-tokens"),
                        },
                        body[:1200],
                    )

                if code == 429:
                    return None, {
                        "provider": active_provider,
                        "attempts": attempts,
                        "used_fallback": True,
                        "last_error": last_error,
                        "http_status": 429,
                    }, headers

                # если это "жёсткая" ошибка — не ретраим
                if code and code not in (408, 409, 425, 429, 500, 502, 503, 504):
                    break

            except (httpx.ReadTimeout, httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = f"network_timeout:{type(e).__name__}"
            except (ValueError, RuntimeError) as e:
                last_error = f"{type(e).__name__}: {e}"
            except Exception as e:
                last_error = f"unexpected:{type(e).__name__}: {e}"

            if retry_idx < int(LLM_MAX_RETRIES):
                sleep_s = _sleep_backoff(retry_idx)
                if time.monotonic() + sleep_s < deadline:
                    await asyncio.sleep(sleep_s)

        # --- Fallback to secondary provider if primary failed ---
        if fallback_provider and time.monotonic() < deadline:
            logger.warning("%s failed (%s), trying fallback provider=%s",
                           active_provider, last_error, fallback_provider)
            try:
                system_prompt = _SYSTEM_PROMPT_LEGACY if schema == "legacy" else _SYSTEM_PROMPT_UI
                remaining = deadline - time.monotonic()
                attempt_timeout = min(float(LLM_ATTEMPT_TIMEOUT_S), max(0.5, remaining))

                obj = await _llm_chat_json(
                    domain_prompt,
                    timeout_s=attempt_timeout,
                    system_prompt=system_prompt,
                    max_tokens=max_tokens_hint,
                    provider=fallback_provider,
                )

                if schema == "legacy":
                    ok, analysis, msg = validate_analysis_json(obj)
                    if ok and analysis is not None:
                        return analysis.to_dict(), {
                            "provider": fallback_provider,
                            "attempts": attempts + 1,
                            "used_fallback": True,
                            "last_error": None,
                        }, {}
                else:
                    obj2 = _normalize_ui_obj(schema, obj)
                    ok, analysis2, msg = validate_ui_json(schema, obj2)
                    if ok and analysis2 is not None:
                        return analysis2, {
                            "provider": fallback_provider,
                            "attempts": attempts + 1,
                            "used_fallback": True,
                            "last_error": None,
                        }, {}
            except Exception as fb_err:
                logger.error(
                    "Fallback %s also failed: %s: %s",
                    fallback_provider, type(fb_err).__name__, fb_err,
                    exc_info=True,
                )
                last_error = f"fallback_{fallback_provider}_failed:{type(fb_err).__name__}:{fb_err}"

        return None, {"provider": active_provider, "attempts": attempts, "used_fallback": True, "last_error": last_error}, {}

    obj, gmeta = await gw.run(
        user_id=int(user_id or 0),
        kind=kind,
        cache_key=cache_key,
        prompt=domain_prompt,
        max_tokens=max_tokens_hint,
        call_llm_fn=_call_llm,
        ttl_s=int(ttl_s),
    )

    meta = {
        "provider": gmeta.provider,
        "attempts": 1,
        "elapsed_ms": gmeta.elapsed_ms,
        "used_fallback": bool(gmeta.used_fallback),
        "last_error": gmeta.last_error,
        "cache": gmeta.cache,
    }

    if obj is None:
        if schema == "legacy":
            return fallback_analysis(domain_prompt), meta
        return _fallback_ui(schema, "AI временно недоступен — показываю базовую справку."), meta

    if schema == "legacy":
        ok, analysis, msg = validate_analysis_json(obj)
        if ok and analysis is not None:
            meta["used_fallback"] = False
            meta["last_error"] = None
            return analysis, meta
        meta["used_fallback"] = True
        meta["last_error"] = f"post_validate_failed:{msg}"
        return fallback_analysis(domain_prompt), meta

    meta["used_fallback"] = False
    meta["last_error"] = None
    return obj, meta


# -----------------------------
# Backward compatibility
# -----------------------------
async def analyze_with_llm(
    domain_prompt: str,
    *,
    schema: str = "legacy",
    user_id: Optional[int] = None,
) -> Tuple[LLMOutput, dict]:
    cache_key = f"no_cache:{hash((schema, domain_prompt))}"
    return await analyze_with_llm_cached(domain_prompt, cache_key=cache_key, ttl_s=0, schema=schema, user_id=user_id)


# -----------------------------
# Legacy renderer (telegram)
# -----------------------------
def render_analysis_text(a: LLMAnalysis) -> str:
    verdict_map = {"lean_yes": "🟢 Скорее ДА", "lean_no": "🔴 Скорее НЕТ", "unclear": "⚪️ Неясно"}
    lines: list[str] = []
    lines.append("🧠 AI разбор (LLM)")
    lines.append(f"Вердикт: {verdict_map.get(a.verdict, a.verdict)}")
    lines.append(f"Уверенность: {int(a.confidence * 100)}%")
    lines.append("")
    lines.append("Ключевые мысли:")
    for b in a.reasoning_bullets[:5]:
        lines.append(f"• {b}")
    if a.risks:
        lines.append("")
        lines.append("Риски:")
        for r in a.risks[:5]:
            lines.append(f"• {r}")
    if a.checklist:
        lines.append("")
        lines.append("Чек-лист перед решением:")
        for c in a.checklist[:5]:
            lines.append(f"• {c}")
    lines.append("")
    lines.append("Дисклеймер: это аналитика, не рекомендация к ставкам.")
    return "\n".join(lines)
