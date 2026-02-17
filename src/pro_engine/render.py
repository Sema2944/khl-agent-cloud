# src/pro_engine/render.py
"""
Telegram text renderer for PRO ENGINE 2.0.
Produces the formatted LIVE PRO block — no LLM required.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DISCLAIMER = "ℹ️ Аналитический материал."


def render_pro_message(
    snapshot: Dict[str, Any],
    signals: Dict[str, Any],
    scenarios: Dict[str, Any],
) -> str:
    """
    Render full PRO LIVE Telegram message.
    Never raises — returns fallback on error.
    """
    try:
        lines = []

        # ── Header ──────────────────────────────────────────
        lines.append("🟢 LIVE PRO")
        lines.append("")

        teams = snapshot.get("teams") or {}
        home = teams.get("home") or "Хозяева"
        away = teams.get("away") or "Гости"
        lines.append(f"{home} — {away}")

        league = snapshot.get("league") or ""
        country = snapshot.get("country") or ""
        league_line = " • ".join(p for p in [league, country] if p)
        if league_line:
            lines.append(league_line)

        status_line = _render_status(snapshot)
        if status_line:
            lines.append(status_line)

        # ── 📊 Control (MCI) ────────────────────────────────
        mci = signals.get("mci") or {}
        if mci.get("inputs_used"):
            lines.append("")
            lines.append("📊 Control")
            mci_val = mci.get("value", 50)
            mci_winner = mci.get("winner", "–")
            mci_explain = mci.get("explain", "")
            bullet = f"• MCI: {mci_val}/100 → {mci_winner}"
            if mci_explain:
                bullet += f" ({mci_explain})"
            lines.append(bullet)

        # ── 📈 Pressure / Momentum ───────────────────────────
        stats = snapshot.get("stats") or {}
        shots_h = stats.get("shots_home") or stats.get("shots_on_goal_home")
        shots_a = stats.get("shots_away") or stats.get("shots_on_goal_away")
        momentum = signals.get("momentum") or {}
        mom_val = momentum.get("value")
        mom_explain = momentum.get("explain", "")

        has_pressure = (shots_h is not None and shots_a is not None) or mom_val is not None
        if has_pressure:
            lines.append("")
            lines.append("📈 Pressure / Momentum")
            if shots_h is not None and shots_a is not None:
                lines.append(f"• Shots: {shots_h}–{shots_a}")
            if mom_val is not None:
                sign = "+" if mom_val > 0 else ""
                lines.append(f"• Momentum: {sign}{mom_val}% ({mom_explain})" if mom_explain else f"• Momentum: {sign}{mom_val}%")

        # ── 📉 Market ────────────────────────────────────────
        market = signals.get("market") or {}
        ml_h = market.get("ml_home")
        ml_a = market.get("ml_away")
        market_status = market.get("status", "")
        market_explain = market.get("explain", "")
        delta_imp = market.get("delta_implied_home")

        if ml_h is not None and ml_a is not None:
            lines.append("")
            lines.append("📉 Market")
            lines.append(f"• ML: Хозяева {ml_h} / Гости {ml_a}")
            if market_status and market_status not in ("нет данных", "стабильно"):
                drift_line = f"• Drift: {market_status}"
                if market_explain:
                    drift_line += f" — {market_explain}"
                lines.append(drift_line)
            elif delta_imp is not None:
                sign = "+" if delta_imp >= 0 else ""
                lines.append(f"• Движение: {sign}{round(delta_imp * 100)}% к хозяевам")

        # ── 🎯 Сценарий (main) ───────────────────────────────
        main_sc = scenarios.get("main")
        alt_sc = scenarios.get("alt")
        cancel_txt = scenarios.get("cancel") or ""

        if main_sc:
            lines.append("")
            lines.append(f"🎯 Сценарий: {main_sc.get('name', '—')}")
            if main_sc.get("description"):
                lines.append(f"• {main_sc['description']}")
            if main_sc.get("trigger"):
                lines.append(f"• Триггер: {main_sc['trigger']}")

        # ── 🔁 Альтернатива ──────────────────────────────────
        if alt_sc:
            lines.append("")
            lines.append(f"🔁 Альтернатива: {alt_sc.get('name', '—')}")
            if alt_sc.get("description"):
                lines.append(f"• {alt_sc['description']}")
            if alt_sc.get("trigger"):
                lines.append(f"• Триггер: {alt_sc['trigger']}")

        # ── ⛔ Отмена ─────────────────────────────────────────
        if cancel_txt:
            lines.append("")
            lines.append(f"⛔ Отмена: {cancel_txt}")

        # ── 🛡 Risk / Confidence ─────────────────────────────
        risk = signals.get("risk") or {}
        conf = signals.get("confidence") or {}
        data_flags = signals.get("data_flags") or {}

        risk_val = risk.get("value", "–")
        conf_val = conf.get("value", "–")
        risk_factors = risk.get("factors") or []

        lines.append("")
        lines.append("🛡 Risk / Confidence")
        lines.append(f"• Risk: {risk_val}/5{' — ' + '; '.join(risk_factors) if risk_factors else ''}")
        lines.append(f"• Confidence: {conf_val}/5")

        # Data availability line
        have = [k for k, v in data_flags.items() if v]
        miss = [k for k, v in data_flags.items() if not v]
        data_parts = []
        if have:
            data_parts.append("✓ " + ", ".join(have))
        if miss:
            data_parts.append("✗ " + ", ".join(miss))
        if data_parts:
            lines.append("• Data: " + " | ".join(data_parts))

        # ── Footer ───────────────────────────────────────────
        lines.append("")
        lines.append(_DISCLAIMER)

        return "\n".join(lines)

    except Exception:
        logger.exception("render_pro_message failed")
        return "🟢 LIVE PRO\n\nОшибка отрисовки — попробуй позже.\n\nℹ️ Аналитический материал."


# ---------------------------------------------------------------------------
# Status line helper
# ---------------------------------------------------------------------------

def _render_status(snapshot: Dict[str, Any]) -> str:
    clock = snapshot.get("clock") or {}
    score = snapshot.get("score") or {}
    status = snapshot.get("status") or ""

    parts = []

    # Raw status word
    raw_status = clock.get("raw") or status
    if raw_status:
        parts.append(str(raw_status).capitalize())

    # Period
    period = clock.get("period")
    if period:
        parts.append(f"П{period}")

    # Minute
    minute = clock.get("minute")
    if minute is not None:
        parts.append(f"{minute}'")

    # Score
    s_h = score.get("home")
    s_a = score.get("away")
    if s_h is not None and s_a is not None:
        parts.append(f"Счёт: {s_h}:{s_a}")

    return " | ".join(parts)
