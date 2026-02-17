# src/pro_engine/__init__.py
"""
PRO ENGINE 2.0 — детерминированный LIVE Decision Engine.
Pipeline: raw → snapshot → signals → scenarios → render (без LLM).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .snapshot import build_snapshot
from .signals import compute_signals
from .scenarios import build_scenarios
from .render import render_pro_message

logger = logging.getLogger(__name__)


def run_pro_engine(
    match_meta: Dict[str, Any],
    prev_snapshot: Optional[Dict[str, Any]] = None,
    cur_snapshot: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Main entry point.
    Returns formatted Telegram text for PRO LIVE block.
    Never raises — returns fallback string on any error.
    """
    try:
        raw = match_meta.get("raw") or {}
        odds_raw = (cur_snapshot or {}).get("odds") or {}
        if not (isinstance(odds_raw, dict) and odds_raw.get("markets")):
            odds_raw = match_meta.get("odds_base") or {}

        # 1) Normalize
        snapshot = build_snapshot(raw, odds_raw, match_meta)

        # 2) Signals (with prev snapshot for deltas)
        prev_eng_snap = None
        if isinstance(prev_snapshot, dict) and prev_snapshot.get("_pro_engine"):
            prev_eng_snap = prev_snapshot["_pro_engine"]

        signals = compute_signals(snapshot, prev_eng_snap)

        # 3) Scenarios
        scenarios = build_scenarios(snapshot, signals)

        # 4) Render
        text = render_pro_message(snapshot, signals, scenarios)

        logger.info(
            "PRO engine OK: match=%s mci=%s/%s risk=%s/5 conf=%s/5",
            snapshot.get("match_id"),
            signals.get("mci", {}).get("winner"),
            signals.get("mci", {}).get("value"),
            signals.get("risk", {}).get("value"),
            signals.get("confidence", {}).get("value"),
        )

        return text

    except Exception:
        logger.exception("PRO engine run_pro_engine failed")
        return "🟢 LIVE PRO\n\nОшибка движка — попробуй позже.\n\nℹ️ Аналитический материал."
