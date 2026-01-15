# src/llm_client.py
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Optional, Tuple, Dict, Union

import httpx

from .llm_gateway import get_gateway

logger = logging.getLogger(__name__)

# -----------------------------
# ENV
# -----------------------------
LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "openai").strip().lower()  # openai | dummy
LLM_ENABLED = (os.getenv("LLM_ENABLED") or "1").strip() == "1"

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_BASE_URL = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/")
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()

# Timeouts / retries
# Было 12/8/1 — часто не хватает на большие промпты с oddsBase.
LLM_TOTAL_TIMEOUT_S = float((os.getenv("LLM_TOTAL_TIMEOUT_S") or "45.0").strip())
LLM_ATTEMPT_TIMEOUT_S = float((os.getenv("LLM_ATTEMPT_TIMEOUT_S") or "25.0").strip())
LLM_MAX_RETRIES = int((os.getenv("LLM_MAX_RETRIES") or "2").strip())

OPENAI_TEMPERATURE = float((os.getenv("OPENAI_TEMPERATURE") or "0.1").strip())

# TTLs (используются как подсказка gateway)
LLM_CACHE_TTL_S = int((os.getenv("LLM_CACHE_TTL_S") or "900").strip())          # prematch 15 минут
LLM_CACHE_TTL_LIVE_S = int((os.getenv("LLM_CACHE_TTL_LIVE_S") or "25").strip()) # live 25 сек

# Safety throttles (Telegram-friendly) — лёгкая локальная защита
LLM_PER_USER_MIN_INTERVAL_S = float((os.getenv("LLM_PER_USER_MIN_INTERVAL_S") or "2.5").strip())

LLMOutput = Union["LLMAnalysis", Dict[str, Any]]

_ALLOWED_VERDICTS = {"lean_yes", "lean_no", "unclear"}

_BANNED_PHRASES = (
    "ставь", "ставьте", "бери", "берите", "выгодно", "лучше", "проход", "верняк",
    "гарант", "гарантия", "100%", "фикс"
)

# -----------------------------
# Per-user throttle state (process-local)
# -----------------------------
_PER_USER_LAST_CALL: Dict[int, float] = {}
_PER_USER_LOCK = asyncio.Lock()


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
def _contains_banned_phrases(obj: Any) -> bool:
    try:
        s = json.dumps(obj, ensure_ascii=False).lower()
    except Exception:
        s = str(obj).lower()
    return any(p in s for p in _BANNED_PHRASES)


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

    title = str(out.get("title") or "").strip()
    if not title:
        out["title"] = "🟢 LIVE-обзор" if schema == "ui_live" else "📊 Обзор рынков"

    disclaimer = str(out.get("disclaimer") or "").strip()
    if not disclaimer:
        out["disclaimer"] = "Аналитический материал, не является рекомендацией."

    if "risks" in out and not isinstance(out["risks"], list):
        out["risks"] = _as_str_list(out.get("risks"), max_len=6)

    if schema == "ui_live":
        if "context" in out and not isinstance(out["context"], list):
            out["context"] = _as_str_list(out.get("context"), max_len=6)
        mk = out.get("markets")
        if mk is None or not isinstance(mk, list):
            out["markets"] = []
    else:
        if "key_factors" in out and not isinstance(out["key_factors"], list):
            out["key_factors"] = _as_str_list(out.get("key_factors"), max_len=6)
        if "line_logic" in out and not isinstance(out["line_logic"], list):
            out["line_logic"] = _as_str_list(out.get("line_logic"), max_len=6)

        summary = str(out.get("summary") or "").strip()
        if not summary and not out.get("key_factors") and not out.get("line_logic"):
            out["summary"] = "Короткий разбор недоступен — показываю базовую структуру."

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
    # ui_* иногда требует больше, чтобы модель успела вернуть валидный JSON.
    if schema in ("ui_pre", "ui_live"):
        return 520
    return 300


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

    disclaimer = str(obj.get("disclaimer", "")).strip()
    if not disclaimer:
        return False, None, "disclaimer is empty"

    risks = _as_str_list(obj.get("risks"), max_len=6)

    if schema == "ui_live":
        context = _as_str_list(obj.get("context"), max_len=6)
        markets = obj.get("markets") or []
        if not isinstance(markets, list):
            return False, None, "markets is not list"

        mk_out: list[dict] = []
        for it in markets[:4]:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name", "")).strip() or "Market"
            direction = str(it.get("direction", "")).strip() or "unknown"
            logic = str(it.get("logic", "")).strip()
            mk_out.append({"name": name, "direction": direction, "logic": logic})

        if not context and not mk_out:
            return False, None, "context and markets are empty"

        return True, {
            "title": title,
            "context": context,
            "markets": mk_out,
            "risks": risks,
            "disclaimer": disclaimer,
        }, "ok"

    # ui_pre
    summary = str(obj.get("summary", "")).strip()
    key_factors = _as_str_list(obj.get("key_factors"), max_len=6)
    line_logic = _as_str_list(obj.get("line_logic"), max_len=6)

    if not summary and not key_factors and not line_logic:
        return False, None, "summary/key_factors/line_logic are empty"

    return True, {
        "title": title,
        "summary": summary,
        "key_factors": key_factors,
        "line_logic": line_logic,
        "risks": risks,
        "disclaimer": disclaimer,
    }, "ok"


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


def _fallback_ui(schema: str, reason: str) -> Dict[str, Any]:
    if schema == "ui_live":
        return {
            "title": "🟢 LIVE-обзор",
            "context": [reason],
            "markets": [],
            "risks": ["Недостаточно данных для детального LIVE-разбора."],
            "disclaimer": "Аналитический материал, не является рекомендацией.",
        }
    return {
        "title": "📊 Обзор рынков",
        "summary": reason,
        "key_factors": [],
        "line_logic": [],
        "risks": ["Недостаточно данных для детального разбора."],
        "disclaimer": "Аналитический материал, не является рекомендацией.",
    }


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
# OpenAI call
# -----------------------------
async def _openai_chat_json(domain_prompt: str, timeout_s: float, system_prompt: str, max_tokens: int) -> Dict[str, Any]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    payload = {
        "model": OPENAI_MODEL,
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
        r = await client.post(f"{OPENAI_BASE_URL}/chat/completions", headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise ValueError("empty OpenAI response content")

    obj = json.loads(content)
    if not isinstance(obj, dict):
        raise ValueError("model JSON root is not an object")
    return obj


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
                "provider": "openai",
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

                obj = await _openai_chat_json(
                    domain_prompt,
                    timeout_s=attempt_timeout,
                    system_prompt=system_prompt,
                    max_tokens=max_tokens_hint,
                )

                if _contains_banned_phrases(obj):
                    raise ValueError("banned_phrases_in_output")

                if schema == "legacy":
                    ok, analysis, msg = validate_analysis_json(obj)
                    if not ok or analysis is None:
                        raise ValueError(f"schema validation failed: {msg}")
                    return analysis.to_dict(), {
                        "provider": "openai",
                        "attempts": attempts,
                        "used_fallback": False,
                        "last_error": None,
                    }, {}

                obj2 = _normalize_ui_obj(schema, obj)
                ok, analysis2, msg = validate_ui_json(schema, obj2)
                if not ok or analysis2 is None:
                    raise ValueError(f"ui schema validation failed: {msg}")

                return analysis2, {
                    "provider": "openai",
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
                        "OpenAI HTTP 429. headers=%s body=%s",
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
                        "provider": "openai",
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

        return None, {"provider": "openai", "attempts": attempts, "used_fallback": True, "last_error": last_error}, {}

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
