# src/llm_client.py
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Optional, Tuple, Dict, Union

import httpx

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
LLM_TOTAL_TIMEOUT_S = float((os.getenv("LLM_TOTAL_TIMEOUT_S") or "12.0").strip())
LLM_ATTEMPT_TIMEOUT_S = float((os.getenv("LLM_ATTEMPT_TIMEOUT_S") or "8.0").strip())
LLM_MAX_RETRIES = int((os.getenv("LLM_MAX_RETRIES") or "1").strip())

OPENAI_TEMPERATURE = float((os.getenv("OPENAI_TEMPERATURE") or "0.1").strip())

# Cache (in-memory) — потом заменим на Redis
LLM_CACHE_TTL_S = int((os.getenv("LLM_CACHE_TTL_S") or "900").strip())          # prematch 15 минут
LLM_CACHE_TTL_LIVE_S = int((os.getenv("LLM_CACHE_TTL_LIVE_S") or "25").strip()) # live 25 сек

# Safety throttles (Telegram-friendly)
LLM_GLOBAL_QPS = float((os.getenv("LLM_GLOBAL_QPS") or "1.5").strip())  # ~90 RPM max globally
LLM_PER_USER_MIN_INTERVAL_S = float((os.getenv("LLM_PER_USER_MIN_INTERVAL_S") or "5").strip())

# Cooldown after 429 (seconds)
LLM_429_COOLDOWN_DEFAULT_S = int((os.getenv("LLM_429_COOLDOWN_DEFAULT_S") or "600").strip())
LLM_429_COOLDOWN_MAX_S = int((os.getenv("LLM_429_COOLDOWN_MAX_S") or "3600").strip())

LLMOutput = Union["LLMAnalysis", Dict[str, Any]]

_CACHE: Dict[str, Tuple[float, LLMOutput, dict]] = {}
_CACHE_LOCK = asyncio.Lock()

_ALLOWED_VERDICTS = {"lean_yes", "lean_no", "unclear"}

_BANNED_PHRASES = (
    "ставь", "ставьте", "бери", "берите", "выгодно", "лучше", "проход", "верняк",
    "гарант", "гарантия", "100%", "фикс"
)

# -----------------------------
# Rate limiting / cooldown state (process-local)
# -----------------------------
_GLOBAL_LAST_CALL_TS = 0.0
_GLOBAL_LOCK = asyncio.Lock()

_PER_USER_LAST_CALL: Dict[int, float] = {}
_PER_USER_LOCK = asyncio.Lock()

_COOLDOWN_UNTIL_TS = 0.0
_COOLDOWN_REASON = ""
_COOLDOWN_LOCK = asyncio.Lock()


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
    """
    Делает UI-ответ устойчивым:
    - если disclaimer пустой -> подставляем дефолт
    - если title пустой -> подставляем дефолт
    - если поля не того типа -> приводим к безопасным
    """
    out = dict(obj or {})

    # title
    title = str(out.get("title") or "").strip()
    if not title:
        out["title"] = "🟢 LIVE-обзор" if schema == "ui_live" else "📊 Обзор рынков"

    # disclaimer
    disclaimer = str(out.get("disclaimer") or "").strip()
    if not disclaimer:
        out["disclaimer"] = "Аналитический материал, не является рекомендацией."

    # risks
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


def _sleep_jitter(base: float) -> float:
    return base + random.random() * 0.12


def _now() -> float:
    return time.monotonic()


def _wall_now() -> float:
    return time.time()


def _parse_retry_after_seconds(headers: Dict[str, str]) -> Optional[float]:
    ra = headers.get("retry-after")
    if not ra:
        return None
    try:
        return float(ra)
    except Exception:
        return None


def _parse_try_again_seconds_from_body(body_text: str) -> Optional[int]:
    """
    OpenAI error часто содержит: "Please try again in 8h50m29.759s"
    Парсим в секунды.
    """
    if not body_text:
        return None
    m = re.search(r"try again in\s+([0-9hms\.\s]+)", body_text, flags=re.IGNORECASE)
    if not m:
        return None
    s = m.group(1).strip().lower().replace(" ", "")
    # Examples: 8h50m29.759s / 10m5s / 30s
    hours = minutes = seconds = 0.0
    mh = re.search(r"(\d+(?:\.\d+)?)h", s)
    mm = re.search(r"(\d+(?:\.\d+)?)m", s)
    ms = re.search(r"(\d+(?:\.\d+)?)s", s)
    if mh:
        hours = float(mh.group(1))
    if mm:
        minutes = float(mm.group(1))
    if ms:
        seconds = float(ms.group(1))
    total = int(hours * 3600 + minutes * 60 + seconds)
    return total if total > 0 else None


async def _global_rate_limit_wait() -> None:
    """
    Глобальный QPS limiter. Если LLM_GLOBAL_QPS=1.5 -> минимум 0.666s между вызовами.
    """
    if LLM_GLOBAL_QPS <= 0:
        return
    min_interval = 1.0 / LLM_GLOBAL_QPS
    async with _GLOBAL_LOCK:
        global _GLOBAL_LAST_CALL_TS
        now = _now()
        delta = now - _GLOBAL_LAST_CALL_TS
        if delta < min_interval:
            await asyncio.sleep(min_interval - delta)
        _GLOBAL_LAST_CALL_TS = _now()


async def _per_user_throttle(user_id: int) -> Optional[float]:
    """
    Возвращает seconds_to_wait, если надо подождать (или None).
    """
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


async def _cooldown_left() -> Tuple[float, str]:
    async with _COOLDOWN_LOCK:
        left = _COOLDOWN_UNTIL_TS - _wall_now()
        return (left if left > 0 else 0.0), _COOLDOWN_REASON


async def _set_cooldown(seconds: int, reason: str) -> None:
    sec = max(1, int(seconds))
    sec = min(sec, LLM_429_COOLDOWN_MAX_S)
    async with _COOLDOWN_LOCK:
        global _COOLDOWN_UNTIL_TS, _COOLDOWN_REASON
        _COOLDOWN_UNTIL_TS = _wall_now() + sec
        _COOLDOWN_REASON = reason


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
    """
    schema:
      - ui_pre  -> {title, summary, key_factors, line_logic, risks, disclaimer}
      - ui_live -> {title, context, markets, risks, disclaimer}
    """
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
        for it in markets[:3]:
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
            "Сверь коэффициенты и движение линии (если есть), избегай решений «на эмоциях».",
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
async def _openai_chat_json(domain_prompt: str, timeout_s: float, system_prompt: str) -> Dict[str, Any]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    payload = {
        "model": OPENAI_MODEL,
        "temperature": OPENAI_TEMPERATURE,
        # 🔥 Главное: заставляем API вернуть валидный JSON
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _make_user_prompt(domain_prompt)},
        ],
        "max_tokens": 260,
    }

    timeout = httpx.Timeout(
        timeout_s,
        connect=min(8.0, timeout_s),
        read=timeout_s,
        write=min(8.0, timeout_s),
        pool=min(8.0, timeout_s),
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{OPENAI_BASE_URL}/chat/completions", headers=headers, json=payload)
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            # log 429 details nicely
            if e.response is not None and e.response.status_code == 429:
                try:
                    body = e.response.text
                except Exception:
                    body = ""
                h = {k.lower(): v for k, v in (e.response.headers or {}).items()}
                logger.warning("OpenAI HTTP error 429. headers=%s body=%s", {
                    "retry-after": h.get("retry-after"),
                    "x-ratelimit-limit-requests": h.get("x-ratelimit-limit-requests"),
                    "x-ratelimit-limit-tokens": h.get("x-ratelimit-limit-tokens"),
                    "x-ratelimit-remaining-requests": h.get("x-ratelimit-remaining-requests"),
                    "x-ratelimit-remaining-tokens": h.get("x-ratelimit-remaining-tokens"),
                    "x-ratelimit-reset-requests": h.get("x-ratelimit-reset-requests"),
                    "x-ratelimit-reset-tokens": h.get("x-ratelimit-reset-tokens"),
                    "x-request-id": h.get("x-request-id"),
                }, body[:2000])
            raise

        data = r.json()

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise ValueError("empty OpenAI response content")

    obj = json.loads(content)
    if not isinstance(obj, dict):
        raise ValueError("model JSON root is not an object")
    return obj


# -----------------------------
# LLM main
# -----------------------------
async def analyze_with_llm(domain_prompt: str, *, schema: str = "legacy", user_id: Optional[int] = None) -> Tuple[LLMOutput, dict]:
    """
    user_id optional — чтобы включить per-user throttle в телеге.
    """
    start = time.monotonic()
    attempts = 0
    last_error: Optional[str] = None

    # Cooldown guard (after 429)
    left, reason = await _cooldown_left()
    if left > 0.01:
        meta = {
            "provider": "openai",
            "attempts": 0,
            "elapsed_ms": int((time.monotonic() - start) * 1000),
            "used_fallback": True,
            "last_error": f"cooldown_http_429:{left:.1f}s" if reason else f"cooldown:{left:.1f}s",
        }
        if schema == "legacy":
            return fallback_analysis(domain_prompt), meta
        if schema == "ui_live":
            return {
                "title": "🟢 LIVE-обзор",
                "context": ["AI временно недоступен — лимиты OpenAI (429)."],
                "markets": [],
                "risks": ["Подожди немного и повтори позже."],
                "disclaimer": "Аналитический материал, не является рекомендацией.",
            }, meta
        return {
            "title": "📊 Обзор рынков",
            "summary": "AI временно недоступен — лимиты OpenAI (429).",
            "key_factors": [],
            "line_logic": [],
            "risks": ["Подожди немного и повтори позже."],
            "disclaimer": "Аналитический материал, не является рекомендацией.",
        }, meta

    # Disabled
    if not LLM_ENABLED:
        meta = {
            "provider": "disabled",
            "attempts": 0,
            "elapsed_ms": int((time.monotonic() - start) * 1000),
            "used_fallback": True,
            "last_error": "LLM_ENABLED=0",
        }
        if schema == "legacy":
            return fallback_analysis(domain_prompt), meta
        if schema == "ui_live":
            return {
                "title": "🟢 LIVE-обзор",
                "context": ["AI отключён (LLM_ENABLED=0)."],
                "markets": [],
                "risks": ["Недостаточно данных для детального LIVE-разбора."],
                "disclaimer": "Аналитический материал, не является рекомендацией.",
            }, meta
        return {
            "title": "📊 Обзор рынков",
            "summary": "AI отключён (LLM_ENABLED=0).",
            "key_factors": [],
            "line_logic": [],
            "risks": ["Недостаточно данных для детального разбора."],
            "disclaimer": "Аналитический материал, не является рекомендацией.",
        }, meta

    # Dummy
    if LLM_PROVIDER == "dummy":
        meta = {
            "provider": "dummy",
            "attempts": 0,
            "elapsed_ms": int((time.monotonic() - start) * 1000),
            "used_fallback": True,
            "last_error": "LLM_PROVIDER=dummy",
        }
        if schema == "legacy":
            return fallback_analysis(domain_prompt), meta
        if schema == "ui_live":
            return {
                "title": "🟢 LIVE-обзор",
                "context": ["LLM_PROVIDER=dummy — безопасный режим."],
                "markets": [],
                "risks": ["Отключён внешний LLM."],
                "disclaimer": "Аналитический материал, не является рекомендацией.",
            }, meta
        return {
            "title": "📊 Обзор рынков",
            "summary": "LLM_PROVIDER=dummy — безопасный режим.",
            "key_factors": [],
            "line_logic": [],
            "risks": ["Отключён внешний LLM."],
            "disclaimer": "Аналитический материал, не является рекомендацией.",
        }, meta

    # Per-user throttle (for Telegram)
    if user_id is not None:
        wait_s = await _per_user_throttle(int(user_id))
        if wait_s and wait_s > 0.01:
            meta = {
                "provider": "openai",
                "attempts": 0,
                "elapsed_ms": int((time.monotonic() - start) * 1000),
                "used_fallback": True,
                "last_error": f"throttle_user:{wait_s:.1f}s",
            }
            if schema == "legacy":
                return fallback_analysis(domain_prompt), meta
            if schema == "ui_live":
                return {
                    "title": "🟢 LIVE-обзор",
                    "context": ["Слишком частые запросы — подожди пару секунд."],
                    "markets": [],
                    "risks": ["Telegram-friendly throttle."],
                    "disclaimer": "Аналитический материал, не является рекомендацией.",
                }, meta
            return {
                "title": "📊 Обзор рынков",
                "summary": "Слишком частые запросы — подожди пару секунд.",
                "key_factors": [],
                "line_logic": [],
                "risks": ["Telegram-friendly throttle."],
                "disclaimer": "Аналитический материал, не является рекомендацией.",
            }, meta

    deadline = start + max(0.1, LLM_TOTAL_TIMEOUT_S)

    for retry_idx in range(LLM_MAX_RETRIES + 1):
        attempts += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0.05:
            last_error = "deadline_exceeded"
            break

        attempt_timeout = min(LLM_ATTEMPT_TIMEOUT_S, max(0.3, remaining))

        try:
            # Global QPS limit before calling OpenAI
            await _global_rate_limit_wait()

            system_prompt = _SYSTEM_PROMPT_LEGACY if schema == "legacy" else _SYSTEM_PROMPT_UI
            obj = await _openai_chat_json(domain_prompt, timeout_s=attempt_timeout, system_prompt=system_prompt)

            if _contains_banned_phrases(obj):
                raise ValueError("banned_phrases_in_output")

            if schema == "legacy":
                ok, analysis, msg = validate_analysis_json(obj)
                if not ok or analysis is None:
                    raise ValueError(f"schema validation failed: {msg}")
                return analysis, {
                    "provider": "openai",
                    "attempts": attempts,
                    "elapsed_ms": int((time.monotonic() - start) * 1000),
                    "used_fallback": False,
                    "last_error": None,
                }

            obj2 = _normalize_ui_obj(schema, obj)
            ok, analysis2, msg = validate_ui_json(schema, obj2)
            if not ok or analysis2 is None:
                raise ValueError(f"ui schema validation failed: {msg}")

            return analysis2, {
                "provider": "openai",
                "attempts": attempts,
                "elapsed_ms": int((time.monotonic() - start) * 1000),
                "used_fallback": False,
                "last_error": None,
            }

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_error = f"network_timeout: {type(e).__name__}"
        except httpx.HTTPStatusError as e:
            code = e.response.status_code if e.response is not None else 0
            if code == 429 and e.response is not None:
                # compute cooldown
                h = {k.lower(): v for k, v in (e.response.headers or {}).items()}
                retry_after = _parse_retry_after_seconds(h) or 0.0
                body_text = ""
                try:
                    body_text = e.response.text or ""
                except Exception:
                    body_text = ""
                try_again = _parse_try_again_seconds_from_body(body_text) or 0

                # Prefer "try again in ..." if huge, but cap to MAX_S
                cooldown = 0
                if try_again > 0:
                    cooldown = min(try_again, LLM_429_COOLDOWN_MAX_S)
                elif retry_after > 0:
                    cooldown = int(min(retry_after, LLM_429_COOLDOWN_MAX_S))
                else:
                    cooldown = LLM_429_COOLDOWN_DEFAULT_S

                await _set_cooldown(cooldown, reason="http_429")
                last_error = f"http_429:cooldown_for:{cooldown}s headers:{ {k: h.get(k) for k in ('retry-after','x-ratelimit-limit-requests','x-ratelimit-limit-tokens','x-ratelimit-remaining-requests','x-ratelimit-remaining-tokens','x-ratelimit-reset-requests','x-ratelimit-reset-tokens','x-request-id')} }"
                # no point retrying immediately if 429
                break

            last_error = f"http_{code}"
            # на 4xx (кроме ретраимых) — прекращаем
            if code not in (408, 409, 425, 429, 500, 502, 503, 504):
                break
        except (ValueError, RuntimeError) as e:
            last_error = f"{type(e).__name__}: {e}"
        except Exception as e:
            last_error = f"unexpected: {type(e).__name__}: {e}"

        if retry_idx < LLM_MAX_RETRIES:
            sleep_s = _sleep_jitter(0.2)
            if time.monotonic() + sleep_s < deadline:
                await asyncio.sleep(sleep_s)

    # fallback
    if schema == "legacy":
        a = fallback_analysis(domain_prompt)
        return a, {
            "provider": "openai",
            "attempts": attempts,
            "elapsed_ms": int((time.monotonic() - start) * 1000),
            "used_fallback": True,
            "last_error": last_error,
        }

    if schema == "ui_live":
        return {
            "title": "🟢 LIVE-обзор",
            "context": ["AI временно недоступен — показываю базовое объяснение."],
            "markets": [],
            "risks": ["Недостаточно данных для детального LIVE-разбора."],
            "disclaimer": "Аналитический материал, не является рекомендацией.",
        }, {
            "provider": "openai",
            "attempts": attempts,
            "elapsed_ms": int((time.monotonic() - start) * 1000),
            "used_fallback": True,
            "last_error": last_error,
        }

    return {
        "title": "📊 Обзор рынков",
        "summary": "AI временно недоступен — показываю базовую справку.",
        "key_factors": [],
        "line_logic": [],
        "risks": ["Недостаточно данных для детального разбора."],
        "disclaimer": "Аналитический материал, не является рекомендацией.",
    }, {
        "provider": "openai",
        "attempts": attempts,
        "elapsed_ms": int((time.monotonic() - start) * 1000),
        "used_fallback": True,
        "last_error": last_error,
    }


# -----------------------------
# Cache wrapper
# -----------------------------
async def analyze_with_llm_cached(
    domain_prompt: str,
    *,
    cache_key: str,
    ttl_s: Optional[int] = None,
    schema: str = "legacy",
    user_id: Optional[int] = None,
) -> Tuple[LLMOutput, dict]:
    """
    Cached wrapper. cache_key обязателен.
    Хранит (analysis, meta) в памяти процесса.
    """
    now = time.time()
    if ttl_s is None:
        ttl_s = LLM_CACHE_TTL_LIVE_S if schema == "ui_live" else LLM_CACHE_TTL_S

    async with _CACHE_LOCK:
        hit = _CACHE.get(cache_key)
        if hit:
            exp_ts, analysis, meta = hit
            if exp_ts > now:
                meta2 = dict(meta)
                meta2["cache"] = "hit"
                return analysis, meta2
            else:
                _CACHE.pop(cache_key, None)

    analysis, meta = await analyze_with_llm(domain_prompt, schema=schema, user_id=user_id)
    meta2 = dict(meta)
    meta2["cache"] = "miss"

    async with _CACHE_LOCK:
        _CACHE[cache_key] = (now + ttl_s, analysis, meta2)

    return analysis, meta2


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
