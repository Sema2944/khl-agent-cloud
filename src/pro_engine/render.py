# src/pro_engine/render.py
"""
Telegram text renderer for PRO ENGINE 2.0.
Produces the formatted LIVE PRO block — no LLM required.
Sport-aware: adapts emoji, terminology and stats per sport.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DISCLAIMER = "ℹ️ Аналитический материал."

try:
    from zoneinfo import ZoneInfo
    _MSK = ZoneInfo("Europe/Moscow")
except Exception:
    _MSK = None


# ---------------------------------------------------------------------------
# Sport-specific terminology
# ---------------------------------------------------------------------------

_SPORT_TERMS: Dict[str, Dict[str, Any]] = {
    "ice-hockey": {
        "emoji": "🏒",
        "period_label": "П",          # Период
        "score_event": "гол",
        "penalty_event": "удаление",
        "pressure_stat": "броски",
        "pressure_label": "Shots",
        "dominance_word": "давит",
    },
    "football": {
        "emoji": "⚽",
        "period_label": "Т",           # Тайм
        "score_event": "гол",
        "penalty_event": "удаление",
        "pressure_stat": "удары",
        "pressure_label": "Shots",
        "dominance_word": "давит",
    },
    "basketball": {
        "emoji": "🏀",
        "period_label": "Q",           # Четверть
        "score_event": "рывок",
        "penalty_event": "фол",
        "pressure_stat": "атаки",
        "pressure_label": "Scoring",
        "dominance_word": "доминирует",
    },
    "tennis": {
        "emoji": "🎾",
        "period_label": "Сет",
        "score_event": "брейк",
        "penalty_event": "двойная ошибка",
        "pressure_stat": "подачи",
        "pressure_label": "Serve",
        "dominance_word": "доминирует",
    },
    "volleyball": {
        "emoji": "🏐",
        "period_label": "Сет",
        "score_event": "серия очков",
        "penalty_event": "ошибка",
        "pressure_stat": "атаки",
        "pressure_label": "Attacks",
        "dominance_word": "доминирует",
    },
}

_DEFAULT_TERMS: Dict[str, Any] = {
    "emoji": "🏟",
    "period_label": "П",
    "score_event": "гол",
    "penalty_event": "нарушение",
    "pressure_stat": "действия",
    "pressure_label": "Actions",
    "dominance_word": "давит",
}


def _terms(sport_slug: str) -> Dict[str, Any]:
    return _SPORT_TERMS.get(sport_slug or "", _DEFAULT_TERMS)


# ---------------------------------------------------------------------------
# Main renderer
# ---------------------------------------------------------------------------

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
        lines: List[str] = []

        sport_slug = snapshot.get("sport_slug") or "ice-hockey"
        t = _terms(sport_slug)

        teams = snapshot.get("teams") or {}
        home = teams.get("home") or "Хозяева"
        away = teams.get("away") or "Гости"
        clock = snapshot.get("clock") or {}
        score = snapshot.get("score") or {}
        stats = snapshot.get("stats") or {}

        # ── Header with period/time ──────────────────────────
        header_parts = ["🟢 LIVE PRO"]
        period = clock.get("period")
        minute = clock.get("minute")
        if period:
            header_parts.append(f"{t['period_label']}{period}")
        if minute is not None:
            header_parts.append(f"{minute}'")
        lines.append(" | ".join(header_parts))
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")

        # ── Score line ──────────────────────────────────────
        s_h = score.get("home")
        s_a = score.get("away")
        score_txt = f"{s_h}:{s_a}" if (s_h is not None and s_a is not None) else "–:–"
        quarters = score.get("quarters")
        if quarters:
            q_parts = ", ".join(f"{h}:{a}" for h, a in quarters)
            score_txt += f" ({q_parts})"
        lines.append(f"{t['emoji']} {home} {score_txt} {away}")

        league = snapshot.get("league") or ""
        country = snapshot.get("country") or ""
        league_line = " • ".join(p for p in [league, country] if p)
        if league_line:
            lines.append(f"   {league_line}")

        # ── Confidence ──
        conf = signals.get("confidence") or {}
        conf_val = conf.get("value", 1)

        # ── 📊 Статистика матча ────────────────────────────
        _render_stats_section(lines, stats, sport_slug)

        # ── ⚡ События ──────────────────────────────────────
        _render_events_section(lines, snapshot.get("events") or [])

        # ── 📊 Control (MCI) ────────────────────────────────
        mci = signals.get("mci") or {}
        mci_val = mci.get("value")
        if mci_val is not None and mci.get("inputs_used"):
            lines.append("")
            lines.append("📊 Control")
            mci_winner = mci.get("winner", "–")
            mci_explain = mci.get("explain", "")
            bullet = f"• MCI: {mci_val}/100 → {mci_winner}"
            if mci_explain:
                bullet += f" ({mci_explain})"
            lines.append(bullet)

        # ── 📈 Pressure / Momentum ───────────────────────────
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
                lines.append(f"• {t['pressure_label']}: {shots_h}–{shots_a}")
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
            lines.append("📈 Коэффициенты LIVE")
            lines.append(f"  П1: {ml_h}  |  П2: {ml_a}")

            # Total
            odds = snapshot.get("odds") or {}
            t_line = odds.get("total_line")
            t_over = odds.get("total_over")
            t_under = odds.get("total_under")
            if t_line is not None:
                parts = [f"  ТБ {t_line}: {t_over or '—'}"]
                if t_under is not None:
                    parts.append(f"ТМ {t_line}: {t_under}")
                lines.append("  |  ".join(parts))

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
        data_flags = signals.get("data_flags") or {}

        risk_val = risk.get("value", "–")
        risk_factors = risk.get("factors") or []
        conf_explain = conf.get("explain", "")

        lines.append("")
        lines.append("🛡 Risk / Confidence")
        lines.append(f"• Risk: {risk_val}/5{' — ' + '; '.join(risk_factors) if risk_factors else ''}")
        conf_line = f"• Confidence: {conf_val}/5"
        if conf_explain:
            conf_line += f" ({conf_explain})"
        lines.append(conf_line)

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

        # ── Low-data hint (inline, not blocking) ─────────────
        if conf_val <= 2:
            lines.append("")
            lines.append("💡 Данные обновятся по ходу матча — нажми 🔄")

        # ── Footer ───────────────────────────────────────────
        lines.append("")
        lines.append(_DISCLAIMER)
        _render_timestamp(lines)

        return "\n".join(lines)

    except Exception:
        logger.exception("render_pro_message failed")
        return "🟢 LIVE PRO\n\nОшибка отрисовки — попробуй позже.\n\nℹ️ Аналитический материал."


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------

def _render_stats_section(lines: List[str], stats: Dict[str, Any], sport_slug: str = "ice-hockey") -> None:
    """Render statistics table — adapted per sport."""
    stat_lines = []

    if sport_slug == "tennis":
        _render_tennis_stats(stat_lines, stats)
    elif sport_slug == "basketball":
        _render_basketball_stats(stat_lines, stats)
    elif sport_slug == "volleyball":
        _render_volleyball_stats(stat_lines, stats)
    elif sport_slug == "football":
        _render_football_stats(stat_lines, stats)
    else:
        _render_hockey_stats(stat_lines, stats)

    if stat_lines:
        lines.append("")
        lines.append("📊 Статистика матча:")
        lines.extend(stat_lines)


def _render_hockey_stats(stat_lines: List[str], stats: Dict[str, Any]) -> None:
    shots_h = stats.get("shots_home") or stats.get("shots_on_goal_home")
    shots_a = stats.get("shots_away") or stats.get("shots_on_goal_away")
    if shots_h is not None and shots_a is not None:
        dominant = ""
        if shots_h > shots_a * 1.3:
            dominant = " (хозяева давят)"
        elif shots_a > shots_h * 1.3:
            dominant = " (гости давят)"
        stat_lines.append(f"  Броски:     {shots_h} — {shots_a}{dominant}")

    pen_h = stats.get("penalties_home")
    pen_a = stats.get("penalties_away")
    if pen_h is not None and pen_a is not None:
        stat_lines.append(f"  Удаления:   {pen_h} — {pen_a}")

    pp_h = stats.get("pp_home")
    pp_a = stats.get("pp_away")
    if pp_h is not None and pp_a is not None:
        stat_lines.append(f"  Большинство: {pp_h} — {pp_a}")


def _render_football_stats(stat_lines: List[str], stats: Dict[str, Any]) -> None:
    shots_h = stats.get("shots_home") or stats.get("shots_on_goal_home")
    shots_a = stats.get("shots_away") or stats.get("shots_on_goal_away")
    if shots_h is not None and shots_a is not None:
        dominant = ""
        if shots_h > shots_a * 1.3:
            dominant = " (хозяева давят)"
        elif shots_a > shots_h * 1.3:
            dominant = " (гости давят)"
        stat_lines.append(f"  Удары:      {shots_h} — {shots_a}{dominant}")

    poss_h = stats.get("possession_home")
    poss_a = stats.get("possession_away")
    if poss_h is not None and poss_a is not None:
        stat_lines.append(f"  Владение:   {poss_h}% — {poss_a}%")

    corners_h = stats.get("corners_home")
    corners_a = stats.get("corners_away")
    if corners_h is not None and corners_a is not None:
        stat_lines.append(f"  Угловые:    {corners_h} — {corners_a}")

    xg_h = stats.get("xg_home")
    xg_a = stats.get("xg_away")
    if xg_h is not None and xg_a is not None:
        stat_lines.append(f"  xG:         {xg_h} — {xg_a}")

    da_h = stats.get("dangerous_home")
    da_a = stats.get("dangerous_away")
    if da_h is not None and da_a is not None:
        stat_lines.append(f"  Опасные:    {da_h} — {da_a}")


def _render_basketball_stats(stat_lines: List[str], stats: Dict[str, Any]) -> None:
    shots_h = stats.get("shots_home") or stats.get("shots_on_goal_home")
    shots_a = stats.get("shots_away") or stats.get("shots_on_goal_away")
    if shots_h is not None and shots_a is not None:
        stat_lines.append(f"  Атаки:      {shots_h} — {shots_a}")

    pen_h = stats.get("penalties_home")
    pen_a = stats.get("penalties_away")
    if pen_h is not None and pen_a is not None:
        stat_lines.append(f"  Фолы:       {pen_h} — {pen_a}")


def _render_tennis_stats(stat_lines: List[str], stats: Dict[str, Any]) -> None:
    # Tennis: aces, double faults, break points, serve %
    shots_h = stats.get("shots_home") or stats.get("shots_on_goal_home")
    shots_a = stats.get("shots_away") or stats.get("shots_on_goal_away")
    if shots_h is not None and shots_a is not None:
        stat_lines.append(f"  Эйсы:      {shots_h} — {shots_a}")

    pen_h = stats.get("penalties_home")
    pen_a = stats.get("penalties_away")
    if pen_h is not None and pen_a is not None:
        stat_lines.append(f"  Дабл-фолты: {pen_h} — {pen_a}")

    pp_h = stats.get("pp_home")
    pp_a = stats.get("pp_away")
    if pp_h is not None and pp_a is not None:
        stat_lines.append(f"  Брейк-пойнты: {pp_h} — {pp_a}")


def _render_volleyball_stats(stat_lines: List[str], stats: Dict[str, Any]) -> None:
    shots_h = stats.get("shots_home") or stats.get("shots_on_goal_home")
    shots_a = stats.get("shots_away") or stats.get("shots_on_goal_away")
    if shots_h is not None and shots_a is not None:
        stat_lines.append(f"  Атаки:      {shots_h} — {shots_a}")

    pen_h = stats.get("penalties_home")
    pen_a = stats.get("penalties_away")
    if pen_h is not None and pen_a is not None:
        stat_lines.append(f"  Ошибки:     {pen_h} — {pen_a}")


def _render_events_section(lines: List[str], events: list) -> None:
    """Render recent events section."""
    if not events:
        return
    lines.append("")
    lines.append("⚡ Последние события:")
    for ev in events[:5]:
        lines.append(f"  {ev}")


def _render_timestamp(lines: List[str]) -> None:
    """Add MSK timestamp to footer."""
    try:
        if _MSK:
            now_msk = datetime.now(_MSK)
        else:
            now_msk = datetime.utcnow()
        lines.append(f"⏱ Обновлено: {now_msk.strftime('%H:%M')} MSK")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Status line helper
# ---------------------------------------------------------------------------

def _render_status(snapshot: Dict[str, Any]) -> str:
    sport_slug = snapshot.get("sport_slug") or "ice-hockey"
    t = _terms(sport_slug)
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
        parts.append(f"{t['period_label']}{period}")

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
