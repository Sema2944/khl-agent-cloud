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

# ============================================================
# CONFIG (requirements from TЗ)
# - total budget <= 3s
# - retries + jitter
# - strict JSON validation
# - graceful fallback
# ============================================================

LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "openai").strip().lower()  # openai | dummy
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_BASE_URL = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/")
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()

# hard budget ≤ 3 seconds
LLM_TOTAL_TIMEOUT_S = float((os.getenv("LLM_TOTAL_TIMEOUT_S") or "3.0").strip())
# per-attempt timeout (keep a little room for retries)
LLM_ATTEMPT_TIMEOUT_S = float((os.getenv("LLM_ATTEMPT_TIMEOUT_S") or "1.2").strip())
LLM_MAX_RETRIES = int((os.getenv("LLM_MAX_RETRIES") or "1").strip())  # 1 retry => 2 attempts total

# Optional: improve determinism
OPENAI_TEMPERATURE = float((os.getenv("OPENAI_TEMPERATURE") or "0.2").strip())


# ============================================================
# OUTPUT SCHEMA (minimal, stable)
# ============================================================

@dataclass
class LLMAnalysis:
    """
    Strict validated output.
    Keep this schema stable: you can store it, show it in UI, etc.
    """
    verdict: str                    # "lean_yes" | "lean_no" | "unclear"
    confidence: float               # 0..1
    reasoning_bullets: list[str]    # <= 5 bullets
    risks: list[str]                # <= 5 bullets
    checklist: list[str]            # <= 5 bullets

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "reasoning_bullets": self.reasoning_bullets,
            "risks": self.risks,
            "checklist": self.checklist,
        }


# ============================================================
# VALIDATION
# ============================================================

_ALLOWED_VERDICTS = {"lean_yes", "lean_no", "unclear"}


def _clamp01(x: Any) -> float:
    try:
        v = float(x)
    except Exception:
        return 0.0
    if v < 0:
        return 0.0
    if v > 1:
        return 1.0
    return v


def _as_str_list(v: Any, max_len: int = 5) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        # if model returns one string - split to lines
        parts = [p.strip("•- \t") for p in v.splitlines() if p.strip()]
        return parts[:max_len]
    if isinstance(v, list):
        out: list[str] = []
        for item in v:
            if item is None:
                continue
            out.append(str(item).strip())
        out = [x for x in out if x]
        return out[:max_len]
    return []


def validate_analysis_json(obj: Any) -> Tuple[bool, Optional[LLMAnalysis], str]:
    """
    Strict validation with helpful error text.
    """
    if not isinstance(obj, dict):
        return False, None, "root is not an object"

    verdict = str(obj.get("verdict", "")).strip()
    if verdict not in _ALLOWED_VERDICTS:
        return False, None, f"bad verdict: {verdict!r}"

    confidence = _clamp01(obj.get("confidence", 0.0))

    reasoning = _as_str_list(obj.get("reasoning_bullets"), max_len=5)
    risks = _as_str_list(obj.get("risks"), max_len=5)
    checklist = _as_str_list(obj.get("checklist"), max_len=5)

    # Ensure at least 1 reasoning bullet
    if not reasoning:
        return False, None, "reasoning_bullets is empty"

    analysis = LLMAnalysis(
        verdict=verdict,
        confidence=confidence,
        reasoning_bullets=reasoning,
        risks=risks,
        checklist=checklist,
    )
    return True, analysis, "ok"


# ============================================================
# FALLBACK
# ============================================================

def fallback_analysis(user_prompt: str) -> LLMAnalysis:
    """
    Cheap, deterministic fallback that still returns valid schema.
    """
    base_reason = [
        "LLM недоступен/не успел за SLA — даю безопасный базовый разбор.",
        "Сверь коэффициенты и движение линии (если есть), избегай решений «на эмоциях».",
        "Соблюдай банк-менеджмент: фиксированный % на ставку, без догонов.",
    ]
    risks = [
        "Недостаток входных данных (составы/травмы/мотивация/форма).",
        "Случайность и дисперсия в спорте.",
    ]
    checklist = [
        "Проверь составы/вратарей/ключевых игроков",
        "Проверь календарь (b2b, перелёты)",
        "Сравни линию у 2–3 источников (позже подключим)",
        "Определи max stake по банк-менеджменту",
    ]
    return LLMAnalysis(
        verdict="unclear",
        confidence=0.35,
        reasoning_bullets=base_reason[:5],
        risks=risks[:5],
        checklist=checklist[:5],
    )


# ============================================================
# OPENAI CLIENT (minimal, production-ish)
# Uses Chat Completions for compatibility.
# Ensures JSON-only output by instruction + parsing.
# ============================================================

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
    # Keep concise (speed). Your agent can provide richer context later.
    return (
        "Задача: дай осторожный аналитический разбор, без прямых призывов ставить.\n"
        f"Вход:\n{domain_prompt.strip()}\n"
    )


async def _openai_chat_json(domain_prompt: str, timeout_s: float) -> Dict[str, Any]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": OPENAI_MODEL,
        "temperature": OPENAI_TEMPERATURE,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _make_user_prompt(domain_prompt)},
        ],
        # keep it small for latency
        "max_tokens": 350,
    }

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.post(f"{OPENAI_BASE_URL}/chat/completions", headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()

    # Extract content
    content = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    if not content:
        raise ValueError("empty OpenAI response content")

    # Parse JSON strictly
    try:
        obj = json.loads(content)
    except json.JSONDecodeError as e:
        # Try to recover: sometimes model wraps with whitespace or stray text.
        # We'll attempt to extract first {...} block.
        s = content.strip()
        start = s.find("{")
        end = s.rfind("}")
        if start >= 0 and end > start:
            obj = json.loads(s[start : end + 1])
        else:
            raise ValueError(f"invalid JSON from model: {e}") from e

    if not isinstance(obj, dict):
        raise ValueError("model JSON root is not an object")

    return obj


# ============================================================
# RETRY / TIME BUDGET WRAPPER
# ============================================================

def _sleep_jitter(base: float) -> float:
    # small jitter to avoid thundering herd
    return base + random.random() * 0.12


async def analyze_with_llm(domain_prompt: str) -> Tuple[LLMAnalysis, dict]:
    """
    Main entrypoint.

    Returns:
      (analysis, meta)
    Where meta includes:
      provider, attempts, elapsed_ms, used_fallback, last_error
    """
    start = time.monotonic()
    attempts = 0
    last_error: Optional[str] = None

    # quick exit if provider disabled
    if LLM_PROVIDER == "dummy":
        analysis = fallback_analysis(domain_prompt)
        return analysis, {
            "provider": "dummy",
            "attempts": 0,
            "elapsed_ms": int((time.monotonic() - start) * 1000),
            "used_fallback": True,
            "last_error": "LLM_PROVIDER=dummy",
        }

    # total budget guard
    deadline = start + max(0.1, LLM_TOTAL_TIMEOUT_S)

    for retry_idx in range(LLM_MAX_RETRIES + 1):
        attempts += 1

        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0.05:
            last_error = "deadline_exceeded"
            break

        attempt_timeout = min(LLM_ATTEMPT_TIMEOUT_S, max(0.2, remaining))

        try:
            obj = await _openai_chat_json(domain_prompt, timeout_s=attempt_timeout)
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

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_error = f"network_timeout: {type(e).__name__}"
        except httpx.HTTPStatusError as e:
            # 429 / 5xx => retry, 4xx other => usually no
            code = e.response.status_code
            last_error = f"http_{code}"
            if code not in (408, 409, 425, 429, 500, 502, 503, 504):
                break
        except (ValueError, RuntimeError) as e:
            last_error = f"{type(e).__name__}: {e}"
            # for JSON/schema errors - one retry can help, but don't loop too long
        except Exception as e:
            last_error = f"unexpected: {type(e).__name__}: {e}"

        # Retry if we still have time and retries
        if retry_idx < LLM_MAX_RETRIES:
            # short jitter sleep, but don't exceed deadline
            sleep_s = _sleep_jitter(0.15)
            if time.monotonic() + sleep_s < deadline:
                await asyncio.sleep(sleep_s)

    # Fallback
    analysis = fallback_analysis(domain_prompt)
    return analysis, {
        "provider": "openai",
        "attempts": attempts,
        "elapsed_ms": int((time.monotonic() - start) * 1000),
        "used_fallback": True,
        "last_error": last_error,
    }


# ============================================================
# Formatting helper (optional)
# ============================================================

def render_analysis_text(a: LLMAnalysis) -> str:
    """
    Convert validated JSON output to Telegram-friendly text.
    """
    verdict_map = {
        "lean_yes": "🟢 Скорее ДА",
        "lean_no": "🔴 Скорее НЕТ",
        "unclear": "⚪️ Неясно",
    }
    lines: list[str] = []
    lines.append("🧠 *AI разбор (LLM)*")
    lines.append(f"Вердикт: *{verdict_map.get(a.verdict, a.verdict)}*")
    lines.append(f"Уверенность: *{int(a.confidence * 100)}%*")
    lines.append("")

    lines.append("*Ключевые мысли:*")
    for b in a.reasoning_bullets[:5]:
        lines.append(f"• {b}")

    if a.risks:
        lines.append("")
        lines.append("*Риски:*")
        for r in a.risks[:5]:
            lines.append(f"• {r}")

    if a.checklist:
        lines.append("")
        lines.append("*Чек-лист перед решением:*")
        for c in a.checklist[:5]:
            lines.append(f"• {c}")

    lines.append("")
    lines.append("_Дисклеймер: это аналитика, не рекомендация к ставкам._")
    return "\n".join(lines)
