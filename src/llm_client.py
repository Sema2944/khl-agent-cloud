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

logger = logging.getLogger(__name__)

# -----------------------------
# ENV
# -----------------------------
LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "openai").strip().lower()  # openai | dummy
LLM_ENABLED = (os.getenv("LLM_ENABLED") or "1").strip() == "1"

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_BASE_URL = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/")
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()

LLM_TOTAL_TIMEOUT_S = float((os.getenv("LLM_TOTAL_TIMEOUT_S") or "12.0").strip())
LLM_ATTEMPT_TIMEOUT_S = float((os.getenv("LLM_ATTEMPT_TIMEOUT_S") or "8.0").strip())
LLM_MAX_RETRIES = int((os.getenv("LLM_MAX_RETRIES") or "1").strip())  # 1 попытка ретрая ок

OPENAI_TEMPERATURE = float((os.getenv("OPENAI_TEMPERATURE") or "0.1").strip())
OPENAI_MAX_TOKENS = int((os.getenv("OPENAI_MAX_TOKENS") or "260").strip())

# Cache (in-memory) — потом Redis
LLM_CACHE_TTL_S = int((os.getenv("LLM_CACHE_TTL_S") or "900").strip())          # prematch 15 минут
LLM_CACHE_TTL_LIVE_S = int((os.getenv("LLM_CACHE_TTL_LIVE_S") or "20").strip()) # live 20 сек

# Если LLM упал/429 — кэшируем fallback на короткий срок, чтобы не спамить API
LLM_ERROR_TTL_S = int((os.getenv("LLM_ERROR_TTL_S") or "25").strip())

LLMOutput = Union["LLMAnalysis", Dict[str, Any]]
_CACHE: Dict[str, Tuple[float, LLMOutput, dict]] = {}
_CACHE_LOCK = asyncio.Lock()

_ALLOWED_VERDICTS = {"lean_yes", "lean_no", "unclear"}

# Слова, которые могут превратить аналитику в “призыв”
_BANNED_PHRASES = (
    "ставь", "ставьте", "бери", "берите", "верняк",
    "гарант", "гарантия", "100%", "фикс"
)

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
- слова: ставь, ставьте, бери, берите, верняк, гарантия, 100%, фикс

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
    return base + random.random() * 0.25

def _backoff_s(attempt_idx: int, *, base: float = 0.8, cap: float = 6.0) -> float:
    # 0 -> 0.8.., 1 -> 1.6.., 2 -> 3.2.. etc
    val = min(cap, base * (2 ** attempt_idx))
    return _sleep_jitter(val)

def _parse_retry_after(resp: httpx.Response) -> Optional[float]:
    ra = (resp.headers.get("retry-after") or "").strip()
    if not ra:
        return None
    try:
        return float(ra)
    except Exception:
        return None

# -----------------------------
# Models
# -----------------------------
@dataclass
class LLMAnalysis:
    verdict: str
    confidence: float
    reasoning_bullets: list[str]
    risks: list[str]
    checklist: list[str]

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "reasoning_bullets": self.reasoning_bullets,
            "risks": self.risks,
            "checklist": self.checklist,
        }

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
            "Сверь движение линии (если есть) и избегай решений «на эмоциях».",
            "Соблюдай риск-менеджмент: фиксированный % на действие, без догонов.",
        ],
        risks=[
            "Недостаток входных данных (составы/травмы/мотивация/форма).",
            "Случайность и дисперсия в спорте.",
        ],
        checklist=[
            "Проверь составы/ключевых игроков",
            "Проверь календарь (b2b, перелёты)",
            "Сравни линию у 2–3 источников",
            "Определи лимит риска по банку",
        ],
    )

def _make_user_prompt(domain_prompt: str) -> str:
    return (
        "Задача: дай осторожный аналитический разбор, без прямых призывов ставить.\n"
        f"Вход:\n{domain_prompt.strip()}\n"
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
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _make_user_prompt(domain_prompt)},
        ],
        "max_tokens": OPENAI_MAX_TOKENS,
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
        if r.status_code == 429:
            # пробрасываем наружу, чтобы analyze_with_llm сделал правильный backoff
            raise httpx.HTTPStatusError("429 Too Many Requests", request=r.request, response=r)
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
# LLM main
# -----------------------------
async def analyze_with_llm(domain_prompt: str, *, schema: str = "legacy") -> Tuple[LLMOutput, dict]:
    start = time.monotonic()
    attempts = 0
    last_error: Optional[str] = None

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

    deadline = start + max(0.2, LLM_TOTAL_TIMEOUT_S)

    for retry_idx in range(LLM_MAX_RETRIES + 1):
        attempts += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0.08:
            last_error = "deadline_exceeded"
            break

        attempt_timeout = min(LLM_ATTEMPT_TIMEOUT_S, max(0.6, remaining))

        try:
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
            code = e.response.status_code if e.response else 0
            last_error = f"http_{code}"

            # 429: уважаем Retry-After + backoff, НЕ молотим запросами
            if code == 429:
                ra = _parse_retry_after(e.response) if e.response else None
                sleep_s = ra if (ra is not None and ra > 0) else _backoff_s(retry_idx, base=1.2, cap=8.0)
                if retry_idx < LLM_MAX_RETRIES and (time.monotonic() + sleep_s) < deadline:
                    await asyncio.sleep(sleep_s)
                    continue
                break

            # на неретраимые 4xx — прекращаем
            if code and code not in (408, 409, 425, 429, 500, 502, 503, 504):
                break

        except (ValueError, RuntimeError) as e:
            last_error = f"{type(e).__name__}: {e}"

        except Exception as e:
            last_error = f"unexpected: {type(e).__name__}: {e}"

        if retry_idx < LLM_MAX_RETRIES:
            sleep_s = _backoff_s(retry_idx, base=0.6, cap=4.0)
            if time.monotonic() + sleep_s < deadline:
                await asyncio.sleep(sleep_s)

    # fallback
    meta = {
        "provider": "openai",
        "attempts": attempts,
        "elapsed_ms": int((time.monotonic() - start) * 1000),
        "used_fallback": True,
        "last_error": last_error,
    }

    if schema == "legacy":
        return fallback_analysis(domain_prompt), meta

    if schema == "ui_live":
        return {
            "title": "🟢 LIVE-обзор",
            "context": ["AI временно недоступен — показываю базовое объяснение."],
            "markets": [],
            "risks": ["Недостаточно данных для детального LIVE-разбора."],
            "disclaimer": "Аналитический материал, не является рекомендацией.",
        }, meta

    return {
        "title": "📊 Обзор рынков",
        "summary": "AI временно недоступен — показываю базовую справку.",
        "key_factors": [],
        "line_logic": [],
        "risks": ["Недостаточно данных для детального разбора."],
        "disclaimer": "Аналитический материал, не является рекомендацией.",
    }, meta

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
    now = time.time()

    # TTL по умолчанию
    if ttl_s is None:
        ttl_s = LLM_CACHE_TTL_LIVE_S if schema == "ui_live" else LLM_CACHE_TTL_S

    # Cache read
    async with _CACHE_LOCK:
        hit = _CACHE.get(cache_key)
        if hit:
            exp_ts, analysis, meta = hit
            if exp_ts > now:
                meta2 = dict(meta)
                meta2["cache"] = "hit"
                return analysis, meta2
            _CACHE.pop(cache_key, None)

    analysis, meta = await analyze_with_llm(domain_prompt, schema=schema)

    # Если упали и отдали fallback — кладём в кэш на короткий срок, чтобы не спамить API
    used_fallback = bool(meta.get("used_fallback"))
    cache_ttl = (LLM_ERROR_TTL_S if used_fallback else ttl_s)

    meta2 = dict(meta)
    meta2["cache"] = "miss"
    meta2["ttl"] = cache_ttl

    async with _CACHE_LOCK:
        _CACHE[cache_key] = (now + cache_ttl, analysis, meta2)

    return analysis, meta2

# -----------------------------
# Legacy renderer (telegram text)
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
