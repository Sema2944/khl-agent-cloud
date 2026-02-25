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
from .render import render_pro_message, _terms

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

        # Status detection
        status_raw = (snapshot.get("status") or "").lower().strip()

        # Not started — redirect to PRE
        _NOT_STARTED_STATES = {"notstarted", "scheduled", "ns", "fixture", "not started", "tbd", ""}
        if status_raw in _NOT_STARTED_STATES:
            return _render_not_started(snapshot)

        # Finished match — show summary instead of scenarios
        _FINISHED_STATES = {"finished", "ft", "ended", "completed", "final", "after et", "after so", "after ot"}
        if status_raw in _FINISHED_STATES or "finish" in status_raw:
            return _render_finished(snapshot)

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


def _render_not_started(snapshot: Dict[str, Any]) -> str:
    """Message for matches that haven't started yet — redirect to PRE analysis."""
    teams = snapshot.get("teams") or {}
    home = teams.get("home") or "Хозяева"
    away = teams.get("away") or "Гости"
    league = snapshot.get("league") or ""
    country = snapshot.get("country") or ""
    league_line = " • ".join(p for p in [league, country] if p)

    lines = [
        "🟢 LIVE PRO",
        "",
        f"{home} — {away}",
    ]
    if league_line:
        lines.append(league_line)
    lines += [
        "",
        "⏳ Матч ещё не начался.",
        "",
        "LIVE PRO активируется после начала матча.",
        "Посмотри PRE-анализ — там доступны:",
        "• Движение линии (opening → current)",
        "• Форма команд и H2H",
        "• AI-оценка",
        "",
        "👉 Нажми «📈 PRE анализ» для предматчевого разбора.",
    ]
    return "\n".join(lines)


def _render_finished(snapshot: Dict[str, Any]) -> str:
    """Short summary for a finished match — no scenarios needed."""
    sport_slug = snapshot.get("sport_slug") or "ice-hockey"
    t = _terms(sport_slug)

    teams = snapshot.get("teams") or {}
    home = teams.get("home") or "Хозяева"
    away = teams.get("away") or "Гости"
    score = snapshot.get("score") or {}
    s_h = score.get("home")
    s_a = score.get("away")
    score_txt = f"{s_h}:{s_a}" if (s_h is not None and s_a is not None) else "–:–"
    quarters = score.get("quarters")
    if quarters:
        q_parts = ", ".join(f"{h}:{a}" for h, a in quarters)
        score_txt += f" ({q_parts})"

    league = snapshot.get("league") or ""
    country = snapshot.get("country") or ""
    league_line = " • ".join(p for p in [league, country] if p)

    lines = [
        f"{t['emoji']} Матч завершён",
        "",
        f"{home} — {away}",
    ]
    if league_line:
        lines.append(league_line)
    lines += [
        f"Итог: {score_txt}",
        "",
        "ℹ️ LIVE PRO доступен только для матчей в процессе.",
    ]
    return "\n".join(lines)
