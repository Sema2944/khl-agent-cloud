# src/pro_engine/scenarios.py
"""
Scenario Engine: builds main + alternative scenario from snapshot + signals.
Each scenario: {name, description, trigger, cancel, team, confidence}.
Sport-aware: adapts terminology per sport.
Never raises.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sport-specific scenario terminology
# ---------------------------------------------------------------------------

_SCENARIO_TERMS: Dict[str, Dict[str, str]] = {
    "ice-hockey": {
        "score_event": "гол",
        "first_score": "первый гол",
        "penalty_event": "удаление",
        "pressure_stat": "броски",
        "pressure_delta": "Δshots",
        "period_name": "периоде",
        "late_period": "3-й период",
        "fast_counter": "быстрая контратака",
    },
    "football": {
        "score_event": "гол",
        "first_score": "первый гол",
        "penalty_event": "удаление",
        "pressure_stat": "удары",
        "pressure_delta": "Δshots",
        "period_name": "тайме",
        "late_period": "2-й тайм",
        "fast_counter": "быстрая контратака",
    },
    "basketball": {
        "score_event": "рывок",
        "first_score": "первый рывок",
        "penalty_event": "фол",
        "pressure_stat": "атаки",
        "pressure_delta": "Δscoring",
        "period_name": "четверти",
        "late_period": "4-я четверть",
        "fast_counter": "быстрый переход",
    },
    "tennis": {
        "score_event": "брейк",
        "first_score": "первый брейк",
        "penalty_event": "двойная ошибка",
        "pressure_stat": "подачи",
        "pressure_delta": "Δaces",
        "period_name": "сете",
        "late_period": "решающий сет",
        "fast_counter": "брейк на подаче соперника",
    },
    "volleyball": {
        "score_event": "серия очков",
        "first_score": "первая серия",
        "penalty_event": "ошибка",
        "pressure_stat": "атаки",
        "pressure_delta": "Δattacks",
        "period_name": "сете",
        "late_period": "решающий сет",
        "fast_counter": "серия подач",
    },
}

_DEFAULT_SCENARIO_TERMS: Dict[str, str] = {
    "score_event": "гол",
    "first_score": "первый гол",
    "penalty_event": "нарушение",
    "pressure_stat": "действия",
    "pressure_delta": "Δactions",
    "period_name": "периоде",
    "late_period": "финальный период",
    "fast_counter": "быстрая атака",
}


def _sterms(sport_slug: str) -> Dict[str, str]:
    return _SCENARIO_TERMS.get(sport_slug or "", _DEFAULT_SCENARIO_TERMS)


def build_scenarios(
    snapshot: Dict[str, Any],
    signals: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Returns:
        {
          "main": ScenarioDict | None,
          "alt": ScenarioDict | None,
          "cancel": str,
        }
    ScenarioDict = {name, description, trigger, cancel, team, confidence}
    """
    try:
        sport_slug = snapshot.get("sport_slug") or "ice-hockey"
        t = _sterms(sport_slug)

        mci = signals.get("mci") or {}
        momentum = signals.get("momentum") or {}
        market = signals.get("market") or {}
        risk = signals.get("risk") or {}

        mci_value = mci.get("value", 50)
        mci_winner = mci.get("winner", "–")  # Хозяева | Гости | Равно
        mci_conf = float(mci.get("confidence", 0.3))

        mom_value = momentum.get("value")  # int or None
        mom_delta_h = momentum.get("delta_home")
        mom_delta_a = momentum.get("delta_away")

        market_status = market.get("status", "")
        drift_team = market.get("drift_team")  # "home" | "away" | None

        score = snapshot.get("score") or {}
        score_h = score.get("home")
        score_a = score.get("away")
        clock = snapshot.get("clock") or {}
        period = clock.get("period")

        teams = snapshot.get("teams") or {}
        home_name = teams.get("home") or "Хозяева"
        away_name = teams.get("away") or "Гости"

        stats = snapshot.get("stats") or {}
        shots_h = stats.get("shots_home") or stats.get("shots_on_goal_home")
        shots_a = stats.get("shots_away") or stats.get("shots_on_goal_away")

        main_scenario = _build_main(
            mci_value, mci_winner, mci_conf,
            mom_value, mom_delta_h, mom_delta_a,
            market_status, drift_team,
            score_h, score_a, period,
            home_name, away_name,
            shots_h, shots_a, t,
        )

        alt_scenario = _build_alt(
            mci_winner, mom_value,
            market_status, drift_team,
            score_h, score_a, period,
            home_name, away_name, t,
        )

        cancel_text = _build_cancel(mci_winner, home_name, away_name, score_h, score_a, t)

        return {
            "main": main_scenario,
            "alt": alt_scenario,
            "cancel": cancel_text,
        }

    except Exception:
        logger.exception("build_scenarios failed")
        return {"main": None, "alt": None, "cancel": "неожиданное изменение игры"}


# ---------------------------------------------------------------------------
# Main scenario
# ---------------------------------------------------------------------------

def _build_main(
    mci_value, mci_winner, mci_conf,
    mom_value, mom_delta_h, mom_delta_a,
    market_status, drift_team,
    score_h, score_a, period,
    home_name, away_name,
    shots_h, shots_a, t,
) -> Optional[Dict[str, Any]]:
    """Dominant team continues to control — or tense draw scenario."""

    # Determine controlling team
    if mci_winner == "Хозяева":
        ctrl_team = home_name
        ctrl_side = "home"
        opp_team = away_name
    elif mci_winner == "Гости":
        ctrl_team = away_name
        ctrl_side = "away"
        opp_team = home_name
    else:
        # Draw — tight game scenario
        return _scenario_tight_game(score_h, score_a, period, home_name, away_name, mci_value, t)

    # Disssonance? Market not following game pressure → value bet
    if "диссонанс" in (market_status or ""):
        return {
            "name": f"Диссонанс: {ctrl_team} давит",
            "description": (
                f"{ctrl_team} контролирует игру по {t['pressure_stat']}, "
                f"но рынок запаздывает с переоценкой — потенциальная value."
            ),
            "trigger": f"следующий {t['score_event']} {ctrl_team}",
            "team": ctrl_side,
            "confidence": round(min(0.80, mci_conf + 0.05), 2),
        }

    # Strong momentum
    if mom_value is not None and abs(mom_value) >= 30:
        delta = mom_delta_h if ctrl_side == "home" else mom_delta_a
        return {
            "name": f"Контроль {ctrl_team}",
            "description": (
                f"{ctrl_team} набирает темп (+{delta} {t['pressure_stat']} за отрезок). "
                f"При сохранении давления возможен {t['score_event']}."
            ),
            "trigger": f"{t['pressure_delta']} ≥ +3 у {ctrl_team} за 5 мин или {t['score_event']}",
            "team": ctrl_side,
            "confidence": round(min(0.85, mci_conf), 2),
        }

    # Market drift aligns with MCI
    if drift_team and (
        (drift_team == "home" and ctrl_side == "home") or
        (drift_team == "away" and ctrl_side == "away")
    ):
        return {
            "name": f"Рынок + игра: {ctrl_team}",
            "description": (
                f"{ctrl_team} лидирует по MCI={mci_value}/100, "
                f"коэффициенты движутся в их пользу — тренд подтверждён."
            ),
            "trigger": f"продолжение доминирования {ctrl_team} по {t['pressure_stat']}",
            "team": ctrl_side,
            "confidence": round(min(0.82, mci_conf), 2),
        }

    # Generic control
    return {
        "name": f"Контроль {ctrl_team}",
        "description": (
            f"{ctrl_team} контролирует матч (MCI {mci_value}/100). "
            f"Сохраняют инициативу по {t['pressure_stat']}."
        ),
        "trigger": f"MCI остаётся > 60 и {t['pressure_delta']} в пользу {ctrl_team}",
        "team": ctrl_side,
        "confidence": round(mci_conf, 2),
    }


def _scenario_tight_game(score_h, score_a, period, home_name, away_name, mci_value, t) -> Dict[str, Any]:
    has_score = score_h is not None and score_a is not None
    score_txt = f"{score_h}:{score_a}" if has_score else "–:–"
    period_txt = f" в {period}-м {t['period_name']}" if period else ""

    goals_scored = has_score and (score_h + score_a) > 0
    score_equal = has_score and score_h == score_a
    score_diff = abs(score_h - score_a) if has_score else 0

    # MCI text — hide when None
    mci_txt = f" MCI ≈ {mci_value}/100." if mci_value is not None else ""

    if not has_score or score_equal:
        # 0:0, 1:1, 2:2 — truly equal
        name = "Равная борьба"
        if goals_scored:
            description = (
                f"Счёт {score_txt}{period_txt}. "
                f"Команды идут вровень — высокая напряжённость.{mci_txt}"
            )
            trigger = f"следующий {t['score_event']} определит ход матча"
        else:
            description = (
                f"Счёт {score_txt}{period_txt}. "
                f"Команды уравновешены по показателям.{mci_txt}"
            )
            trigger = f"{t['first_score']} или {t['penalty_event']} сломает равновесие"
    elif score_diff == 1:
        # 1:0, 0:1, 2:1 — minimal advantage
        leading = home_name if score_h > score_a else away_name
        name = f"Минимальное преимущество {leading}"
        description = (
            f"Счёт {score_txt}{period_txt}. "
            f"{leading} ведёт с минимальным отрывом — интрига сохраняется.{mci_txt}"
        )
        trigger = f"следующий {t['score_event']}: усиление отрыва или возврат к равенству"
    else:
        # 2:0+ — MCI says "equal" but score doesn't
        leading = home_name if score_h > score_a else away_name
        name = f"Лидерство {leading}"
        description = (
            f"Счёт {score_txt}{period_txt}. "
            f"{leading} впереди, но показатели команд близки.{mci_txt}"
        )
        trigger = f"{t['score_event']} проигрывающей команды или усиление доминирования {leading}"

    return {
        "name": name,
        "description": description,
        "trigger": trigger,
        "team": "neutral",
        "confidence": 0.55,
    }


# ---------------------------------------------------------------------------
# Alternative scenario
# ---------------------------------------------------------------------------

def _build_alt(
    mci_winner, mom_value,
    market_status, drift_team,
    score_h, score_a, period,
    home_name, away_name, t,
) -> Optional[Dict[str, Any]]:
    """Reversal / tempo-drop / underdog scenario."""

    if mci_winner == "Хозяева":
        under_team = away_name
        under_side = "away"
    elif mci_winner == "Гости":
        under_team = home_name
        under_side = "home"
    else:
        # Already even — alt is a scoring burst
        has_score = score_h is not None and score_a is not None
        goals_scored = has_score and (score_h + score_a) > 0
        return {
            "name": "Burst одной из команд",
            "description": (
                f"Следующий {t['score_event']} может резко изменить баланс в пользу забившей команды."
                if goals_scored else
                f"При первом {t['score_event']} ситуация может резко измениться."
            ),
            "trigger": f"следующий {t['score_event']}" if goals_scored else t["first_score"],
            "team": "neutral",
            "confidence": 0.45,
        }

    # Disssonance: underdog is who market favours while pressing less
    if "диссонанс" in (market_status or ""):
        return {
            "name": f"Отскок {under_team}",
            "description": (
                f"{under_team} уступают по {t['pressure_stat']}, но рынок ещё не переоценил шансы. "
                f"Один быстрый {t['score_event']} меняет картину."
            ),
            "trigger": f"{t['score_event']} {under_team} или резкий рост коэффициента против них",
            "team": under_side,
            "confidence": 0.42,
        }

    # Momentum fading (low or None)
    if mom_value is None or abs(mom_value) < 20:
        return {
            "name": f"Спад темпа — шанс {under_team}",
            "description": (
                f"Темп давления снижается. {under_team} могут перехватить инициативу "
                f"при следующем {t['penalty_event']} или {t['fast_counter']}."
            ),
            "trigger": f"{t['penalty_event']} лидирующей команды или {t['pressure_delta']} в пользу {under_team}",
            "team": under_side,
            "confidence": 0.40,
        }

    # Late period volatility
    if period and period >= 3:
        has_score = score_h is not None and score_a is not None
        if has_score and abs(score_h - score_a) <= 1:
            return {
                "name": f"Камбэк {under_team} ({t['late_period']})",
                "description": (
                    f"Минимальная разница в счёте в {t['late_period']}. "
                    f"{under_team} вынуждены идти вперёд — возможно обострение."
                ),
                "trigger": f"{t['score_event']} {under_team} + смена коэффициентов",
                "team": under_side,
                "confidence": 0.48,
            }

    return {
        "name": f"Перехват {under_team}",
        "description": (
            f"{under_team} уступают по контролю, но одно стечение обстоятельств "
            f"({t['penalty_event']}, быстрый {t['score_event']}) может изменить баланс."
        ),
        "trigger": f"{t['penalty_event']} или {t['score_event']} {under_team}",
        "team": under_side,
        "confidence": 0.38,
    }


# ---------------------------------------------------------------------------
# Cancel conditions
# ---------------------------------------------------------------------------

def _build_cancel(mci_winner, home_name, away_name, score_h, score_a, t) -> str:
    if mci_winner == "Хозяева":
        ctrl = home_name
        opp = away_name
    elif mci_winner == "Гости":
        ctrl = away_name
        opp = home_name
    else:
        return f"{t['score_event']} {home_name} или {away_name} с последующим изменением momentum"

    parts = [f"{t['score_event']} {opp}"]
    parts.append(f"{t['penalty_event']} {ctrl}")
    parts.append(f"резкий разворот momentum ({t['pressure_delta']} ≥ +5 за 5 мин у соперника)")
    return " / ".join(parts)
