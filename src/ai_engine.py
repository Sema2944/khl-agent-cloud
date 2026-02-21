# src/ai_engine.py
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from .snapshot_builder import build_snapshot
from .prompt_factory import build_prompt
from .fallback import fallback_analysis

logger = logging.getLogger(__name__)


async def run_ai_analysis(
    *,
    llm_client,  # твой src.llm_client
    user_id: int,
    sport: str,
    match: Dict[str, Any],
    mode: str,      # pre|live|live_pro|daily_pro
    action: str,    # overview|markets|...
    odds: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Возвращает (text, meta)
    meta можно использовать для логов/кеша.
    """
    snapshot = build_snapshot(sport=sport, match=match, odds=odds, extra=extra)
    p = build_prompt(mode=mode, action=action, snapshot=snapshot)

    try:
        # ВАЖНО: тут вызываем твоего клиента
        # ожидаю что llm_client имеет async метод вроде:
        # await llm_client.chat(system=..., user=..., temperature=..., max_tokens=...)
        text = await llm_client.chat(
            system=p["system"],
            user=p["user"],
            temperature=0.3,
            max_tokens=700 if mode != "daily_pro" else 900,
        )
        text = (text or "").strip()
        if not text:
            raise RuntimeError("empty_llm_response")
        return text, {"ok": True, "mode": mode}

    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        logger.warning("LLM failed, fallback used: %s", reason, exc_info=True)
        return fallback_analysis(reason=reason, mode=mode, snapshot=snapshot), {
            "ok": False,
            "mode": mode,
            "reason": reason,
        }
