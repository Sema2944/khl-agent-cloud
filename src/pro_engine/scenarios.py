# src/pro_engine/scenarios.py
"""
Scenario Engine: builds main + alternative scenario from snapshot + signals.
Each scenario: {name, description, trigger, cancel, team, confidence}.
Never raises.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


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
            shots_h, shots_a,
        )

        alt_scenario = _build_alt(
            mci_winner, mom_value,
            market_status, drift_team,
            score_h, score_a, period,
            home_name, away_name,
        )

        cancel_text = _build_cancel(mci_winner, home_name, away_name, score_h, score_a)

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
    shots_h, shots_a,
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
        return _scenario_tight_game(score_h, score_a, period, home_name, away_name, mci_value)

    # Disssonance? Market not following game pressure → value bet
    if "диссонанс" in (market_status or ""):
        return {
            "name": f"Диссонанс: {ctrl_team} давит",
            "description": (
                f"{ctrl_team} контролирует игру по броскам, "
                f"но рынок запаздывает с переоценкой — потенциальная value."
            ),
            "trigger": f"следующий гол {ctrl_team}",
            "team": ctrl_side,
            "confidence": round(min(0.80, mci_conf + 0.05), 2),
        }

    # Strong momentum
    if mom_value is not None and abs(mom_value) >= 30:
        pressing = ctrl_team
        delta = mom_delta_h if ctrl_side == "home" else mom_delta_a
        return {
            "name": f"Контроль {ctrl_team}",
            "description": (
                f"{ctrl_team} набирает темп (+{delta} бросков за отрезок). "
                f"При сохранении давления возможен гол."
            ),
            "trigger": f"Δshots ≥ +3 у {ctrl_team} за 5 мин или гол",
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
            "trigger": f"продолжение доминирования {ctrl_team} по броскам",
            "team": ctrl_side,
            "confidence": round(min(0.82, mci_conf), 2),
        }

    # Generic control
    return {
        "name": f"Контроль {ctrl_team}",
        "description": (
            f"{ctrl_team} контролирует матч (MCI {mci_value}/100). "
            f"Сохраняют инициативу по броскам."
        ),
        "trigger": f"MCI остаётся > {'60' if ctrl_side == 'home' else '60'} и Δshots в пользу {ctrl_team}",
        "team": ctrl_side,
        "confidence": round(mci_conf, 2),
    }


def _scenario_tight_game(score_h, score_a, period, home_name, away_name, mci_value) -> Dict[str, Any]:
    has_score = score_h is not None and score_a is not None
    score_txt = f"{score_h}:{score_a}" if has_score else "–:–"
    tense = has_score and abs(score_h - score_a) <= 1
    period_txt = f" в {period}-м периоде" if period else ""

    return {
        "name": "Равная борьба",
        "description": (
            f"Счёт {score_txt}{period_txt}. "
            f"{'Минимальная разница — высокая напряжённость.' if tense else 'Команды уравновешены по показателям.'} "
            f"MCI ≈ {mci_value}/100."
        ),
        "trigger": "первый гол или удаление сломает равновесие",
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
    home_name, away_name,
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
        return {
            "name": "Burst одной из команд",
            "description": "При первом голе ситуация может резко измениться в пользу забившей команды.",
            "trigger": "первый гол",
            "team": "neutral",
            "confidence": 0.45,
        }

    # Disssonance: underdog is who market favours while pressing less
    if "диссонанс" in (market_status or ""):
        # Market not moving with game → underdog reversal is alternative
        return {
            "name": f"Отскок {under_team}",
            "description": (
                f"{under_team} уступают по броскам, но рынок ещё не переоценил шансы. "
                f"Один быстрый гол меняет картину."
            ),
            "trigger": f"гол {under_team} или резкий рост коэффициента против них",
            "team": under_side,
            "confidence": 0.42,
        }

    # Momentum fading (low or None)
    if mom_value is None or abs(mom_value) < 20:
        return {
            "name": f"Спад темпа — шанс {under_team}",
            "description": (
                f"Темп давления снижается. {under_team} могут перехватить инициативу "
                f"при следующем удалении или быстрой контратаке."
            ),
            "trigger": f"удаление лидирующей команды или Δshots в пользу {under_team}",
            "team": under_side,
            "confidence": 0.40,
        }

    # Late period volatility
    if period and period >= 3:
        has_score = score_h is not None and score_a is not None
        if has_score and abs(score_h - score_a) <= 1:
            return {
                "name": f"Камбэк {under_team} (3-й период)",
                "description": (
                    f"Минимальная разница в счёте в 3-м периоде. "
                    f"{under_team} вынуждены идти вперёд — возможно обострение."
                ),
                "trigger": f"гол {under_team} + смена коэффициентов",
                "team": under_side,
                "confidence": 0.48,
            }

    return {
        "name": f"Перехват {under_team}",
        "description": (
            f"{under_team} уступают по контролю, но одно стечение обстоятельств "
            f"(удаление, быстрый гол) может изменить баланс."
        ),
        "trigger": f"удаление или гол {under_team}",
        "team": under_side,
        "confidence": 0.38,
    }


# ---------------------------------------------------------------------------
# Cancel conditions
# ---------------------------------------------------------------------------

def _build_cancel(mci_winner, home_name, away_name, score_h, score_a) -> str:
    if mci_winner == "Хозяева":
        ctrl = home_name
        opp = away_name
    elif mci_winner == "Гости":
        ctrl = away_name
        opp = home_name
    else:
        return f"гол {home_name} или {away_name} с последующим изменением momentum"

    parts = [f"гол {opp}"]
    parts.append(f"удаление {ctrl}")
    parts.append("резкий разворот momentum (Δshots ≥ +5 за 5 мин у соперника)")
    return " / ".join(parts)
