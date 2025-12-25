# src/llm_client.py
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Optional, Tuple, Dict

import httpx

logger = logging.getLogger(__name__)

LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "openai").strip().lower()  # openai | dummy
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_BASE_URL = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/")
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()

LLM_TOTAL_TIMEOUT_S = float((os.getenv("LLM_TOTAL_TIMEOUT_S") or "3.0").strip())
LLM_ATTEMPT_TIMEOUT_S = float((os.getenv("LLM_ATTEMPT_TIMEOUT_S") or "1.2").strip())
LLM_MAX_RETRIES = int((os.getenv("LLM_MAX_RETRIES") or "1").strip())

OPENAI_TEMPERATURE = float((os.getenv("OPENAI_TEMPERATURE") or "0.2").strip())

# Cache (in-memory) — потом заменим на Redis
LLM_CACHE_TTL_S = int((os.getenv("LLM_CACHE_TTL_S") or "900").strip())  # 15 минут
_CACHE: Dict[str, Tuple[float, "LLMAnalysis", dict]] = {}
_CACHE_LOCK = asyncio.Lock()


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


_ALLOWED_VERDICTS = {"lean_yes", "lean_no", "unclear"}


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


def fallback_analysis(_: str) -> LLMAnalysis:
    return LLMAnalysis(
        verdict="unclear",
        confidence=0.35,
        reasoning_bullets=[
            "LLM недоступен/не успел за SLA — даю безопасный базовый разбор.",
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
            "Сравни линию у 2–3 источников (позже подключим)",
            "Определи max stake по банк-менеджменту",
        ],
    )


_SYSTEM_PROMPT = """Ты спортивный аналитик.
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


def _make_user_prompt(domain_prompt: str) -> str:
    return (
        "Задача: дай осторожный аналитический разбор, без прямых призывов ставить.\n"
        f"Вход:\n{domain_prompt.strip()}\n"
    )


async def _openai_chat_json(domain_prompt: str, timeout_s: float) -> Dict[str, Any]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": OPENAI_MODEL,
        "temperature": OPENAI_TEMPERATURE,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _make_user_prompt(domain_prompt)},
        ],
        "max_tokens": 350,
    }

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.post(f"{OPENAI_BASE_URL}/chat/completions", headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise ValueError("empty OpenAI response content")

    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        s = content.strip()
        start = s.find("{")
        end = s.rfind("}")
        if start >= 0 and end > start:
            obj = json.loads(s[start:end + 1])
        else:
            raise ValueError("invalid JSON from model")

    if not isinstance(obj, dict):
        raise ValueError("model JSON root is not an object")
    return obj


def _sleep_jitter(base: float) -> float:
    return base + random.random() * 0.12


async def analyze_with_llm(domain_prompt: str) -> Tuple[LLMAnalysis, dict]:
    start = time.monotonic()
    attempts = 0
    last_error: Optional[str] = None

    if LLM_PROVIDER == "dummy":
        a = fallback_analysis(domain_prompt)
        return a, {"provider": "dummy", "attempts": 0, "elapsed_ms": int((time.monotonic() - start) * 1000),
                   "used_fallback": True, "last_error": "LLM_PROVIDER=dummy"}

    deadline = start + max(0.1, LLM_TOTAL_TIMEOUT_S)

    for retry_idx in range(LLM_MAX_RETRIES + 1):
        attempts += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0.05:
            last_error = "deadline_exceeded"
            break

        attempt_timeout = min(LLM_ATTEMPT_TIMEOUT_S, max(0.2, remaining))

        try:
            obj = await _openai_chat_json(domain_prompt, timeout_s=attempt_timeout)
            ok, analysis, msg = validate_analysis_json(obj)
            if not ok or analysis is None:
                raise ValueError(f"schema validation failed: {msg}")

            return analysis, {"provider": "openai", "attempts": attempts,
                              "elapsed_ms": int((time.monotonic() - start) * 1000),
                              "used_fallback": False, "last_error": None}

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_error = f"network_timeout: {type(e).__name__}"
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            last_error = f"http_{code}"
            if code not in (408, 409, 425, 429, 500, 502, 503, 504):
                break
        except (ValueError, RuntimeError) as e:
            last_error = f"{type(e).__name__}: {e}"
        except Exception as e:
            last_error = f"unexpected: {type(e).__name__}: {e}"

        if retry_idx < LLM_MAX_RETRIES:
            sleep_s = _sleep_jitter(0.15)
            if time.monotonic() + sleep_s < deadline:
                await asyncio.sleep(sleep_s)

    a = fallback_analysis(domain_prompt)
    return a, {"provider": "openai", "attempts": attempts,
               "elapsed_ms": int((time.monotonic() - start) * 1000),
               "used_fallback": True, "last_error": last_error}


async def analyze_with_llm_cached(
    domain_prompt: str,
    *,
    cache_key: str,
    ttl_s: int = LLM_CACHE_TTL_S,
) -> Tuple[LLMAnalysis, dict]:
    """
    Cached wrapper. cache_key обязателен.
    Хранит (analysis, meta) в памяти процесса.
    """
    now = time.time()

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

    analysis, meta = await analyze_with_llm(domain_prompt)
    meta2 = dict(meta)
    meta2["cache"] = "miss"

    async with _CACHE_LOCK:
        _CACHE[cache_key] = (now + ttl_s, analysis, meta2)

    return analysis, meta2


def render_analysis_text(a: LLMAnalysis) -> str:
    verdict_map = {"lean_yes": "🟢 Скорее ДА", "lean_no": "🔴 Скорее НЕТ", "unclear": "⚪️ Неясно"}
    lines: list[str] = []
    lines.append("🧠 AI разбор (LLM)")  # <- убрал Markdown, чтобы Telegram не падал
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
