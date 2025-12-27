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

LLM_TOTAL_TIMEOUT_S = float((os.getenv("LLM_TOTAL_TIMEOUT_S") or "10.0").strip())
LLM_ATTEMPT_TIMEOUT_S = float((os.getenv("LLM_ATTEMPT_TIMEOUT_S") or "8.0").strip())
LLM_MAX_RETRIES = int((os.getenv("LLM_MAX_RETRIES") or "0").strip())

OPENAI_TEMPERATURE = float((os.getenv("OPENAI_TEMPERATURE") or "0.1").strip())

# Cache (in-memory) — потом заменим на Redis
LLM_CACHE_TTL_S = int((os.getenv("LLM_CACHE_TTL_S") or "900").strip())          # prematch 15 минут
LLM_CACHE_TTL_LIVE_S = int((os.getenv("LLM_CACHE_TTL_LIVE_S") or "20").strip()) # live 20 сек

# Telegram-safe throttling
LLM_DEBOUNCE_S = float((os.getenv("LLM_DEBOUNCE_S") or "2.0").strip())  # защита от спама кнопок
COOLDOWN_BASE_S = float((os.getenv("LLM_COOLDOWN_BASE_S") or "60.0").strip())  # минимум 60с после 429
COOLDOWN_MAX_S = float((os.getenv("LLM_COOLDOWN_MAX_S") or "600.0").strip())   # максимум 10 мин

LLMOutput = Union["LLMAnalysis", Dict[str, Any]]
_CACHE: Dict[str, Tuple[float, LLMOutput, dict]] = {}
_CACHE_LOCK = asyncio.Lock()

_ALLOWED_VERDICTS = {"lean_yes", "lean_no", "unclear"}

# Мягкий список: баним только как отдельные слова/формы (чтобы не ловить "лучше" внутри другого слова)
_BANNED_WORDS = (
    "ставь", "ставьте", "бери", "берите", "выгодно", "проход", "верняк",
    "гарант", "гарантия", "фикс"
)

_BANNED_RE = re.compile(r"(?i)\b(" + "|".join(map(re.escape, _BANNED_WORDS)) + r")\b")

# -----------------------------
# Global throttles (per instance)
# -----------------------------
_LLM_SEM = asyncio.Semaphore(1)  # 1 запрос к OpenAI одновременно

# per-key cooldown after 429
_COOLDOWN_UNTIL_TS: float = 0.0
_COOLDOWN_PENALTY_S: float = 0.0

# small debounce map (cache_key -> last_ts)
_LAST_CALL_TS: Dict[str, float] = {}
_LAST_CALL_LOCK = asyncio.Lock()


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
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:
        s = str(obj)
    return bool(_BANNED_RE.search(s))


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


def _try_extract_json_obj(content: str) -> Dict[str, Any]:
    """
    Мягкий парсер: если модель обернула JSON текстом — вытаскиваем { ... }.
    """
    s = (content or "").strip()
    if not s:
        raise ValueError("empty content")

    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        obj = json.loads(s[start:end + 1])
        if isinstance(obj, dict):
            return obj

    raise ValueError("invalid JSON from model")


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
        if mk is None:
            out["markets"] = []
        elif not isinstance(mk, list):
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
    return base + random.random() * 0.2


def _parse_retry_after_seconds(resp: httpx.Response) -> Optional[float]:
    ra = resp.headers.get("retry-after")
    if not ra:
        return None
    try:
        return float(ra)
    except Exception:
        return None


def _extract_rate_headers(resp: httpx.Response) -> dict:
    # хедеры могут различаться, логируем всё похожее
    out = {}
    for k, v in resp.headers.items():
        lk = k.lower()
        if "ratelimit" in lk or lk in ("retry-after", "x-request-id"):
            out[k] = v
    return out


def _cooldown_left_s() -> float:
    now = time.monotonic()
    left = _COOLDOWN_UNTIL_TS - now
    return left if left > 0 else 0.0


async def _debounce(cache_key: str) -> Optional[float]:
    """
    Возвращает seconds_left если надо задебаунсить, иначе None.
    """
    if not cache_key:
        return None
    now = time.monotonic()
    async with _LAST_CALL_LOCK:
        last = _LAST_CALL_TS.get(cache_key, 0.0)
        if now - last < LLM_DEBOUNCE_S:
            return LLM_DEBOUNCE_S - (now - last)
        _LAST_CALL_TS[cache_key] = now
    return None


def _bump_cooldown(retry_after_s: Optional[float]) -> float:
    """
    После 429 увеличиваем cooldown.
    """
    global _COOLDOWN_UNTIL_TS, _COOLDOWN_PENALTY_S
    now = time.monotonic()

    base = COOLDOWN_BASE_S
    if retry_after_s and retry_after_s > 0:
        base = max(base, retry_after_s)

    if _COOLDOWN_PENALTY_S <= 0:
        _COOLDOWN_PENALTY_S = base
    else:
        _COOLDOWN_PENALTY_S = min(COOLDOWN_MAX_S, max(base, _COOLDOWN_PENALTY_S * 1.5))

    _COOLDOWN_UNTIL_TS = now + _COOLDOWN_PENALTY_S
    return _COOLDOWN_PENALTY_S


def _reset_cooldown_on_success() -> None:
    global _COOLDOWN_PENALTY_S
    # не обнуляем полностью, но уменьшаем, чтобы не дергаться
    if _COOLDOWN_PENALTY_S > 0:
        _COOLDOWN_PENALTY_S = max(0.0, _COOLDOWN_PENALTY_S * 0.5)


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
- слова: ставь, ставьте, бери, берите, выгодно, проход, гарантия, фикс

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
# OpenAI call (chat/completions)
# -----------------------------
async def _openai_chat_json(domain_prompt: str, timeout_s: float, system_prompt: str) -> Dict[str, Any]:
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
        except httpx.HTTPStatusError:
            # логируем детали (особенно для 429)
            body_text = ""
            try:
                body_text = r.text
            except Exception:
                pass
            rate_headers = _extract_rate_headers(r)
            logger.warning(
                "OpenAI HTTP error %s. headers=%s body=%s",
                r.status_code,
                rate_headers,
                (body_text[:1200] if body_text else ""),
            )
            raise

        data = r.json()

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise ValueError("empty OpenAI response content")

    # json_object обычно уже валиден, но на всякий случай — мягкий парс
    obj = _try_extract_json_obj(content)
    if not isinstance(obj, dict):
        raise ValueError("model JSON root is not an object")
    return obj


# -----------------------------
# LLM main
# -----------------------------
async def analyze_with_llm(domain_prompt: str, *, schema: str = "legacy") -> Tuple[LLMOutput, dict]:
    start = time.monotonic()
    attempts = 0
    last_error: Optional[str] = None
    cooldown_left = _cooldown_left_s()
    if cooldown_left > 0:
        # не стучим в OpenAI, сразу fallback
        meta = {
            "provider": "openai",
            "attempts": 0,
            "elapsed_ms": 0,
            "used_fallback": True,
            "last_error": f"cooldown_http_429:{cooldown_left:.1f}s",
            "cooldown_left_s": round(cooldown_left, 1),
        }
        if schema == "legacy":
            return fallback_analysis(domain_prompt), meta
        if schema == "ui_live":
            return {
                "title": "🟢 LIVE-обзор",
                "context": ["AI временно недоступен (429). Попробуй позже."],
                "markets": [],
                "risks": ["Сработал анти-спам лимит провайдера."],
                "disclaimer": "Аналитический материал, не является рекомендацией.",
            }, meta
        return {
            "title": "📊 Обзор рынков",
            "summary": "AI временно недоступен (429). Попробуй позже.",
            "key_factors": [],
            "line_logic": [],
            "risks": ["Сработал анти-спам лимит провайдера."],
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

    deadline = start + max(0.1, LLM_TOTAL_TIMEOUT_S)

    # один запрос к OpenAI одновременно (и кнопки не должны пробивать лимиты)
    async with _LLM_SEM:
        for retry_idx in range(LLM_MAX_RETRIES + 1):
            attempts += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0.05:
                last_error = "deadline_exceeded"
                break

            attempt_timeout = min(LLM_ATTEMPT_TIMEOUT_S, max(0.5, remaining))

            try:
                system_prompt = _SYSTEM_PROMPT_LEGACY if schema == "legacy" else _SYSTEM_PROMPT_UI
                obj = await _openai_chat_json(domain_prompt, timeout_s=attempt_timeout, system_prompt=system_prompt)

                # banned words guard
                if _contains_banned_phrases(obj):
                    raise ValueError("banned_phrases_in_output")

                if schema == "legacy":
                    ok, analysis, msg = validate_analysis_json(obj)
                    if not ok or analysis is None:
                        raise ValueError(f"schema validation failed: {msg}")
                    _reset_cooldown_on_success()
                    return analysis, {
                        "provider": "openai",
                        "attempts": attempts,
                        "elapsed_ms": int((time.monotonic() - start) * 1000),
                        "used_fallback": False,
                        "last_error": None,
                    }

                # UI schemas: normalize then validate
                obj2 = _normalize_ui_obj(schema, obj)
                ok, analysis2, msg = validate_ui_json(schema, obj2)
                if not ok or analysis2 is None:
                    raise ValueError(f"ui schema validation failed: {msg}")

                _reset_cooldown_on_success()
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
                code = e.response.status_code
                rate_headers = _extract_rate_headers(e.response)
                retry_after_s = _parse_retry_after_seconds(e.response)

                if code == 429:
                    cd = _bump_cooldown(retry_after_s)
                    last_error = f"http_429:cooldown_for:{cd:.0f}s headers:{rate_headers}"
                else:
                    last_error = f"http_{code} headers:{rate_headers}"

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
) -> Tuple[LLMOutput, dict]:
    """
    Cached wrapper. cache_key обязателен.
    Хранит (analysis, meta) в памяти процесса.
    + debounce для телеги
    """
    # debounce (против спама кнопок на один и тот же cache_key)
    left = await _debounce(cache_key)
    if left is not None and left > 0:
        meta = {
            "provider": "openai",
            "attempts": 0,
            "elapsed_ms": 0,
            "used_fallback": True,
            "last_error": f"debounce:{left:.1f}s",
            "cache": "miss",
            "debounce_left_s": round(left, 1),
        }
        if schema == "legacy":
            return fallback_analysis(domain_prompt), meta
        if schema == "ui_live":
            return {
                "title": "🟢 LIVE-обзор",
                "context": ["Слишком часто. Подожди пару секунд и повтори."],
                "markets": [],
                "risks": ["Debounce Telegram-кликов."],
                "disclaimer": "Аналитический материал, не является рекомендацией.",
            }, meta
        return {
            "title": "📊 Обзор рынков",
            "summary": "Слишком часто. Подожди пару секунд и повтори.",
            "key_factors": [],
            "line_logic": [],
            "risks": ["Debounce Telegram-кликов."],
            "disclaimer": "Аналитический материал, не является рекомендацией.",
        }, meta

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

    analysis, meta = await analyze_with_llm(domain_prompt, schema=schema)
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
