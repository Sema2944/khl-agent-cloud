# src/pro_engine/signals.py
"""
Signal Engine: MCI, Momentum, Market Movement, Risk, Confidence.
Each signal returns {value, explain, confidence, inputs_used}.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def compute_signals(
    snapshot: Dict[str, Any],
    prev_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Computes all signals from normalized snapshot.
    Never raises.
    """
    try:
        stats = snapshot.get("stats") or {}
        odds = snapshot.get("odds") or {}
        score = snapshot.get("score") or {}
        clock = snapshot.get("clock") or {}

        shots_h = stats.get("shots_home") or stats.get("shots_on_goal_home")
        shots_a = stats.get("shots_away") or stats.get("shots_on_goal_away")
        ml_h = odds.get("ml_home")
        ml_a = odds.get("ml_away")
        score_h = score.get("home")
        score_a = score.get("away")
        period = clock.get("period")
        minute = clock.get("minute")
        pen_h = stats.get("penalties_home")
        pen_a = stats.get("penalties_away")
        pp_h = stats.get("pp_home")
        pp_a = stats.get("pp_away")
        total_line = odds.get("total_line")

        prev_stats = (prev_snapshot or {}).get("stats") or {}
        prev_odds = (prev_snapshot or {}).get("odds") or {}

        has_events = bool(snapshot.get("events"))

        signals = {
            "mci": _compute_mci(shots_h, shots_a, ml_h, ml_a, score_h, score_a),
            "momentum": _compute_momentum(shots_h, shots_a, prev_stats),
            "market": _compute_market(ml_h, ml_a, prev_odds, shots_h, shots_a),
            "risk": _compute_risk(shots_h, shots_a, ml_h, ml_a, score_h, score_a, period, minute, pen_h, pen_a, prev_odds),
            "confidence": _compute_confidence(shots_h, shots_a, ml_h, ml_a, period, minute,
                                               score_h=score_h, score_a=score_a, has_events=has_events),
            "data_flags": _data_flags(shots_h, shots_a, ml_h, ml_a, period, pen_h, pen_a),
        }

        logger.info(
            "PRO signals: mci=%s/%s mom=%s risk=%s/5 conf=%s/5 flags=%s",
            signals["mci"]["winner"], signals["mci"]["value"],
            signals["momentum"].get("value"),
            signals["risk"]["value"], signals["confidence"]["value"],
            signals["data_flags"],
        )

        return signals

    except Exception:
        logger.exception("compute_signals failed")
        return _empty_signals()


# ---------------------------------------------------------------------------
# MCI — Match Control Index 0..100 (home perspective)
# ---------------------------------------------------------------------------

def _compute_mci(shots_h, shots_a, ml_h, ml_a, score_h, score_a) -> Dict[str, Any]:
    inputs_used: List[str] = []
    shot_score = market_score = score_score = 0.0

    has_shots = shots_h is not None and shots_a is not None and (shots_h + shots_a) > 0
    has_ml = ml_h is not None and ml_a is not None and ml_h > 0.1 and ml_a > 0.1
    has_score = score_h is not None and score_a is not None

    if has_shots:
        share = shots_h / (shots_h + shots_a)
        shot_score = (share - 0.5) * 200  # -100..100
        inputs_used.append("shots")

    if has_ml:
        imp_h = 1 / ml_h
        imp_a = 1 / ml_a
        total_imp = imp_h + imp_a
        if total_imp > 0:
            norm_h = imp_h / total_imp
            market_score = _clamp((norm_h - 0.5) * 200, -100, 100)
        inputs_used.append("odds")

    if has_score:
        score_score = _clamp((score_h - score_a) * 15, -30, 30)
        inputs_used.append("score")

    # Rule: MCI requires at least shots or odds — score alone is NOT enough
    # (score 0:0 at 20-5 shots ≠ MCI 50; score alone = scoreboard data, not PRO value)
    if not has_shots and not has_ml:
        return {"value": None, "winner": "–", "explain": "недостаточно данных для MCI (нет бросков и коэффициентов)",
                "confidence": 0.0, "inputs_used": inputs_used}

    if has_shots and has_ml:
        mci = 50 + 0.55 * shot_score + 0.25 * market_score + 0.20 * score_score
    elif has_shots:
        mci = 50 + 0.70 * shot_score + 0.30 * score_score
    elif has_ml:
        mci = 50 + 0.70 * market_score + 0.30 * score_score
    else:
        mci = 50 + score_score  # unreachable due to guard above

    mci = int(round(_clamp(mci, 0, 100)))

    if mci >= 62:
        winner = "Хозяева"
        conf = min(0.95, 0.40 + (mci - 62) / 50)
    elif mci <= 38:
        winner = "Гости"
        conf = min(0.95, 0.40 + (50 - mci) / 50)
    else:
        winner = "Равно"
        conf = 0.30

    parts = []
    if has_shots:
        parts.append(f"броски {shots_h}–{shots_a}")
    if has_ml:
        parts.append(f"коэф. {ml_h}/{ml_a}")
    if has_score:
        parts.append(f"счёт {score_h}:{score_a}")

    return {
        "value": mci,
        "winner": winner,
        "explain": ", ".join(parts),
        "confidence": round(conf, 2),
        "inputs_used": inputs_used,
    }


# ---------------------------------------------------------------------------
# Momentum (delta shots vs prev snapshot)
# ---------------------------------------------------------------------------

def _compute_momentum(shots_h, shots_a, prev_stats: Dict[str, Any]) -> Dict[str, Any]:
    prev_h = prev_stats.get("shots_home") or prev_stats.get("shots_on_goal_home")
    prev_a = prev_stats.get("shots_away") or prev_stats.get("shots_on_goal_away")

    if shots_h is None or shots_a is None or prev_h is None or prev_a is None:
        return {"value": None, "delta_home": None, "delta_away": None,
                "explain": "нет дельты (первый снапшот)", "confidence": 0.0, "inputs_used": []}

    delta_h = shots_h - prev_h
    delta_a = shots_a - prev_a
    mom = delta_h - delta_a  # > 0 → home pressure
    mom_pct = int(round(_clamp(mom * 10, -100, 100)))

    if mom_pct >= 20:
        explain = f"давление хозяев (+{delta_h} бросков)"
    elif mom_pct <= -20:
        explain = f"давление гостей (+{delta_a} бросков)"
    else:
        explain = "нейтральный темп"

    return {
        "value": mom_pct,
        "delta_home": delta_h,
        "delta_away": delta_a,
        "explain": explain,
        "confidence": 0.65,
        "inputs_used": ["shots_delta"],
    }


# ---------------------------------------------------------------------------
# Market Movement
# ---------------------------------------------------------------------------

def _compute_market(ml_h, ml_a, prev_odds: Dict[str, Any], shots_h, shots_a) -> Dict[str, Any]:
    has_odds = ml_h is not None and ml_a is not None and ml_h > 0.1 and ml_a > 0.1

    if not has_odds:
        return {"ml_home": None, "ml_away": None, "status": "нет данных",
                "explain": "коэффициенты недоступны", "drift_team": None,
                "delta_implied_home": None, "confidence": 0.0, "inputs_used": []}

    result: Dict[str, Any] = {
        "ml_home": ml_h, "ml_away": ml_a,
        "status": "стабильно", "explain": "",
        "drift_team": None, "delta_implied_home": None,
        "confidence": 0.50, "inputs_used": ["ml_home", "ml_away"],
    }

    prev_ml_h = prev_odds.get("ml_home") if prev_odds else None
    prev_ml_a = prev_odds.get("ml_away") if prev_odds else None

    if prev_ml_h and prev_ml_a and prev_ml_h > 0.1 and prev_ml_a > 0.1:
        delta_imp_h = round((1 / ml_h) - (1 / prev_ml_h), 3)
        result["delta_implied_home"] = delta_imp_h
        result["inputs_used"].append("ml_prev")

        if delta_imp_h > 0.05:
            result["status"] = "усиливает хозяев"
            result["drift_team"] = "home"
            result["explain"] = f"вероятность хозяев +{round(delta_imp_h * 100)}%"
            result["confidence"] = 0.75
        elif delta_imp_h < -0.05:
            result["status"] = "усиливает гостей"
            result["drift_team"] = "away"
            result["explain"] = f"вероятность гостей +{round(-delta_imp_h * 100)}%"
            result["confidence"] = 0.75

    # Disssonance check
    if shots_h is not None and shots_a is not None and (shots_h + shots_a) > 0:
        sog_ratio = shots_h / max(1, shots_a)
        drift = result["drift_team"]
        if sog_ratio >= 1.5 and drift in ("away", None):
            result["status"] = "диссонанс"
            result["explain"] = "хозяева давят по броскам, рынок не реагирует → value"
            result["confidence"] = 0.80
            result["inputs_used"].append("shots")
        elif sog_ratio <= 0.67 and drift in ("home", None):
            result["status"] = "диссонанс"
            result["explain"] = "гости давят по броскам, рынок не реагирует → value"
            result["confidence"] = 0.80
            result["inputs_used"].append("shots")

    return result


# ---------------------------------------------------------------------------
# Risk 1..5
# ---------------------------------------------------------------------------

def _compute_risk(
    shots_h, shots_a, ml_h, ml_a, score_h, score_a,
    period, minute, pen_h, pen_a, prev_odds: Dict[str, Any]
) -> Dict[str, Any]:
    risk = 1
    factors: List[str] = []
    inputs: List[str] = []

    has_shots = shots_h is not None and shots_a is not None
    has_odds = ml_h is not None and ml_a is not None
    has_clock = period is not None

    if has_shots:
        inputs.append("shots")
    if has_odds:
        inputs.append("odds")
    if has_clock:
        inputs.append("clock")

    if not has_shots and not has_odds:
        risk += 1
        factors.append("нет бросков и коэффициентов")

    if score_h is not None and score_a is not None:
        diff = abs(score_h - score_a)
        if diff <= 1 and period == 3:
            risk += 1
            factors.append(f"напряжённый 3-й период (разница {diff})")
        elif diff == 0 and period is not None and period >= 2:
            risk += 1
            factors.append("ничья, высокая волатильность")

    if has_odds and prev_odds:
        prev_ml_h = prev_odds.get("ml_home")
        if prev_ml_h and prev_ml_h > 0.1 and ml_h > 0.1:
            delta = abs(1 / ml_h - 1 / prev_ml_h)
            if delta > 0.08:
                risk += 1
                factors.append("резкое движение рынка")

    pen_total = (pen_h or 0) + (pen_a or 0)
    if pen_total >= 6:
        risk += 1
        factors.append(f"много удалений ({pen_total} мин)")

    risk = int(_clamp(risk, 1, 5))

    return {
        "value": risk,
        "factors": factors,
        "explain": "; ".join(factors) if factors else "стандартный уровень",
        "confidence": 0.70,
        "inputs_used": inputs,
    }


# ---------------------------------------------------------------------------
# Confidence 1..5
# ---------------------------------------------------------------------------

def _compute_confidence(shots_h, shots_a, ml_h, ml_a, period, minute,
                         score_h=None, score_a=None, has_events: bool = False) -> Dict[str, Any]:
    has_shots = shots_h is not None and shots_a is not None
    has_odds = ml_h is not None and ml_a is not None
    has_clock = period is not None
    has_score = score_h is not None and score_a is not None
    inputs = [x for x, f in [("shots", has_shots), ("odds", has_odds), ("clock", has_clock),
                               ("score", has_score), ("events", has_events)] if f]

    if has_shots and has_odds and has_clock:
        value, explain = 5, "shots + odds + clock"
    elif has_shots and has_clock:
        value, explain = 4, "shots + clock (нет коэффициентов)"
    elif has_shots and has_odds:
        value, explain = 4, "shots + odds (нет времени)"
    elif has_shots:
        value, explain = 3, "только броски"
    elif has_score and has_events and has_clock:
        value, explain = 3, "счёт + события + время (нет бросков)"
    elif has_score and has_clock:
        value, explain = 2, "счёт + время (нет бросков и коэффициентов)"
    elif has_score and has_events:
        value, explain = 2, "счёт + события (нет бросков)"
    elif has_odds:
        value, explain = 2, "только коэффициенты"
    else:
        value, explain = 1, "минимум данных"

    return {"value": value, "explain": explain, "inputs_used": inputs}


# ---------------------------------------------------------------------------
# Data flags
# ---------------------------------------------------------------------------

def _data_flags(shots_h, shots_a, ml_h, ml_a, period, pen_h, pen_a) -> Dict[str, bool]:
    return {
        "shots": shots_h is not None and shots_a is not None,
        "odds": ml_h is not None and ml_a is not None,
        "clock": period is not None,
        "penalties": pen_h is not None and pen_a is not None,
    }


def _empty_signals() -> Dict[str, Any]:
    return {
        "mci": {"value": None, "winner": "–", "explain": "ошибка расчёта", "confidence": 0.0, "inputs_used": []},
        "momentum": {"value": None, "delta_home": None, "delta_away": None, "explain": "недоступно", "confidence": 0.0, "inputs_used": []},
        "market": {"ml_home": None, "ml_away": None, "status": "недоступно", "explain": "", "drift_team": None, "delta_implied_home": None, "confidence": 0.0, "inputs_used": []},
        "risk": {"value": 3, "factors": [], "explain": "неизвестно", "confidence": 0.0, "inputs_used": []},
        "confidence": {"value": 1, "explain": "ошибка расчёта", "inputs_used": []},
        "data_flags": {"shots": False, "odds": False, "clock": False, "penalties": False},
    }
