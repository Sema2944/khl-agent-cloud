# src/live_features.py
"""
Слой 1: Нормализатор данных (extract_live_features)
Слой 2: Rule Engine (build_live_signals)

Работает БЕЗ LLM. Принимает raw dict от SportAPI и odds_raw,
возвращает унифицированные структуры для PRO-промпта.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _i(v: Any) -> Optional[int]:
    """Безопасно приводим к int."""
    if v is None:
        return None
    try:
        return int(float(str(v).strip()))
    except Exception:
        return None


def _f(v: Any) -> Optional[float]:
    """Безопасно приводим к float."""
    if v is None:
        return None
    try:
        return round(float(str(v).strip()), 2)
    except Exception:
        return None


def _dig(d: Any, *path: str) -> Any:
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _first(*vals: Any) -> Any:
    for v in vals:
        if v is not None:
            return v
    return None


# ---------------------------------------------------------------------------
# Слой 1 — Нормализатор данных
# ---------------------------------------------------------------------------

def extract_live_features(
    raw: Dict[str, Any],
    odds_raw: Optional[Dict[str, Any]] = None,
    match_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Принимает raw payload от SportAPI (MatchDTO.raw) и опциональный odds_raw.
    Возвращает унифицированный dict с None для отсутствующих полей.
    Никогда не падает.
    """
    raw = raw or {}
    odds_raw = odds_raw or {}
    match_meta = match_meta or {}

    try:
        # --- период ---
        period = _i(_first(
            raw.get("period"),
            raw.get("currentPeriod"),
            raw.get("current_period"),
            _dig(raw, "time", "period"),
            _dig(raw, "clock", "period"),
            _dig(raw, "status", "period"),
        ))

        # --- минута матча ---
        clock_min = _i(_first(
            raw.get("minute"),
            raw.get("clock"),
            _dig(raw, "time", "minute"),
            _dig(raw, "time", "clock"),
            _dig(raw, "clock", "minute"),
            _dig(raw, "clock", "current"),
        ))

        # --- счёт ---
        score_home = _score_side(raw, "home")
        score_away = _score_side(raw, "away")

        # Если не нашли в raw, попробуем из match_meta.score ("2:1")
        if score_home is None and score_away is None:
            ms = str(match_meta.get("score") or "").strip()
            if ":" in ms:
                parts = ms.split(":")
                try:
                    score_home = int(parts[0].strip())
                    score_away = int(parts[1].strip().split()[0])  # "1 (ФТ)" → 1
                except Exception:
                    pass

        # --- броски (SOG / shots on goal) ---
        sog_home, sog_away = _extract_shots(raw)

        # --- штрафное время / удаления ---
        pen_home, pen_away = _extract_penalties(raw)

        # --- большинство ---
        pp_home, pp_away = _extract_powerplay(raw)

        # --- odds ---
        ml_home, ml_away, total_line, total_over, total_under = _extract_odds(odds_raw or raw)

        return {
            "period": period,
            "clock_min": clock_min,
            "score_home": score_home,
            "score_away": score_away,
            "sog_home": sog_home,
            "sog_away": sog_away,
            "penalties_home": pen_home,
            "penalties_away": pen_away,
            "powerplay_home": pp_home,
            "powerplay_away": pp_away,
            "moneyline_home": ml_home,
            "moneyline_away": ml_away,
            "total_line": total_line,
            "total_over": total_over,
            "total_under": total_under,
        }
    except Exception:
        logger.exception("extract_live_features failed")
        return _empty_features()


def _empty_features() -> Dict[str, Any]:
    return {
        "period": None, "clock_min": None,
        "score_home": None, "score_away": None,
        "sog_home": None, "sog_away": None,
        "penalties_home": None, "penalties_away": None,
        "powerplay_home": None, "powerplay_away": None,
        "moneyline_home": None, "moneyline_away": None,
        "total_line": None, "total_over": None, "total_under": None,
    }


def _score_side(raw: Dict[str, Any], side: str) -> Optional[int]:
    """side = 'home' | 'away'"""
    opp = "away" if side == "home" else "home"
    candidates = [
        raw.get(f"{side}Score"),
        raw.get(f"score_{side}"),
        raw.get(f"goals{side.capitalize()}"),
        _dig(raw, "score", side),
        _dig(raw, "scores", side),
        _dig(raw, "result", side),
        _dig(raw, f"{side}Team", "score"),
        _dig(raw, f"{side}Team", "goals"),
    ]
    for c in candidates:
        v = _i(c)
        if v is not None:
            return v
    return None


def _extract_shots(raw: Dict[str, Any]) -> tuple:
    """Возвращает (sog_home, sog_away)."""
    # Прямые поля
    for key_h, key_a in [
        ("shotsOnGoal_home", "shotsOnGoal_away"),
        ("sog_home", "sog_away"),
        ("shots_home", "shots_away"),
        ("shotsHome", "shotsAway"),
    ]:
        h = _i(raw.get(key_h))
        a = _i(raw.get(key_a))
        if h is not None and a is not None:
            return h, a

    # Вложенные: statistics/stats
    for stats_key in ("statistics", "stats", "shotStats"):
        stats = raw.get(stats_key)
        if isinstance(stats, dict):
            for sk in ("shotsOnGoal", "shots", "sog", "shotsOnTarget"):
                obj = stats.get(sk)
                if isinstance(obj, dict):
                    h = _i(_first(obj.get("home"), obj.get("homeTeam"), obj.get("h")))
                    a = _i(_first(obj.get("away"), obj.get("awayTeam"), obj.get("a")))
                    if h is not None and a is not None:
                        return h, a

        # Список статистик [{type: "shotsOnGoal", home: 12, away: 8}]
        if isinstance(stats, list):
            for item in stats:
                if not isinstance(item, dict):
                    continue
                t = str(item.get("type") or item.get("name") or "").lower()
                if any(x in t for x in ("shot", "sog")):
                    h = _i(_first(item.get("home"), item.get("homeTeam"), item.get("h"), item.get("value_home")))
                    a = _i(_first(item.get("away"), item.get("awayTeam"), item.get("a"), item.get("value_away")))
                    if h is not None and a is not None:
                        return h, a

    return None, None


def _extract_penalties(raw: Dict[str, Any]) -> tuple:
    """Возвращает (pen_min_home, pen_min_away) — штрафные минуты."""
    for key_h, key_a in [
        ("penaltyMinutes_home", "penaltyMinutes_away"),
        ("penalties_home", "penalties_away"),
        ("penMin_home", "penMin_away"),
    ]:
        h = _i(raw.get(key_h))
        a = _i(raw.get(key_a))
        if h is not None and a is not None:
            return h, a

    for stats_key in ("statistics", "stats", "penalties"):
        stats = raw.get(stats_key)
        if isinstance(stats, list):
            for item in stats:
                if not isinstance(item, dict):
                    continue
                t = str(item.get("type") or item.get("name") or "").lower()
                if "penalt" in t or "pim" in t:
                    h = _i(_first(item.get("home"), item.get("h"), item.get("value_home")))
                    a = _i(_first(item.get("away"), item.get("a"), item.get("value_away")))
                    if h is not None and a is not None:
                        return h, a

    return None, None


def _extract_powerplay(raw: Dict[str, Any]) -> tuple:
    """Возвращает (pp_home, pp_away) — количество розыгрышей большинства."""
    for key_h, key_a in [
        ("powerplay_home", "powerplay_away"),
        ("pp_home", "pp_away"),
        ("powerPlays_home", "powerPlays_away"),
    ]:
        h = _i(raw.get(key_h))
        a = _i(raw.get(key_a))
        if h is not None and a is not None:
            return h, a

    for stats_key in ("statistics", "stats"):
        stats = raw.get(stats_key)
        if isinstance(stats, list):
            for item in stats:
                if not isinstance(item, dict):
                    continue
                t = str(item.get("type") or item.get("name") or "").lower()
                if "power" in t or " pp" in t:
                    h = _i(_first(item.get("home"), item.get("h"), item.get("value_home")))
                    a = _i(_first(item.get("away"), item.get("a"), item.get("value_away")))
                    if h is not None and a is not None:
                        return h, a

    return None, None


def _extract_odds(odds_raw: Dict[str, Any]) -> tuple:
    """Возвращает (ml_home, ml_away, total_line, total_over, total_under)."""
    ml_home = ml_away = total_line = total_over = total_under = None

    markets = odds_raw.get("markets")
    if not isinstance(markets, list):
        # Попробуем плоскую структуру
        ml_home = _f(_first(odds_raw.get("moneyline_home"), odds_raw.get("homeOdds"), odds_raw.get("1")))
        ml_away = _f(_first(odds_raw.get("moneyline_away"), odds_raw.get("awayOdds"), odds_raw.get("2")))
        total_line = _f(_first(odds_raw.get("total_line"), odds_raw.get("total"), odds_raw.get("ou_line")))
        total_over = _f(_first(odds_raw.get("total_over"), odds_raw.get("over"), odds_raw.get("ou_over")))
        total_under = _f(_first(odds_raw.get("total_under"), odds_raw.get("under"), odds_raw.get("ou_under")))
        return ml_home, ml_away, total_line, total_over, total_under

    for market in markets:
        if not isinstance(market, dict):
            continue
        name = str(market.get("name") or market.get("marketId") or "").lower()
        choices = market.get("choices") or []
        if not isinstance(choices, list):
            continue

        # Moneyline / 1X2
        if any(x in name for x in ("moneyline", "1x2", "match winner", "result", "winner")):
            for c in choices:
                if not isinstance(c, dict):
                    continue
                cn = str(c.get("name") or "").lower()
                odd = _f(c.get("odd") or c.get("odds") or c.get("price"))
                if any(x in cn for x in ("home", "1", "хозяев")):
                    ml_home = odd
                elif any(x in cn for x in ("away", "2", "гостей")):
                    ml_away = odd

        # Total / Over-Under
        if any(x in name for x in ("total", "over", "under", "goals")):
            for c in choices:
                if not isinstance(c, dict):
                    continue
                cn = str(c.get("name") or "").lower()
                odd = _f(c.get("odd") or c.get("odds") or c.get("price"))
                line = _f(c.get("handicap") or c.get("line") or c.get("points"))
                if "over" in cn or "б" in cn:
                    total_over = odd
                    if line:
                        total_line = line
                elif "under" in cn or "м" in cn:
                    total_under = odd
                    if line and not total_line:
                        total_line = line

    return ml_home, ml_away, total_line, total_over, total_under


# ---------------------------------------------------------------------------
# Слой 2 — Rule Engine
# ---------------------------------------------------------------------------

def build_live_signals(features: Dict[str, Any]) -> Dict[str, Any]:
    """
    Вычисляет аналитические сигналы на основе нормализованных данных.
    Не обращается к LLM. Не падает.
    """
    try:
        sog_h = features.get("sog_home")
        sog_a = features.get("sog_away")
        score_h = features.get("score_home")
        score_a = features.get("score_away")
        period = features.get("period")
        ml_h = features.get("moneyline_home")
        ml_a = features.get("moneyline_away")
        pen_h = features.get("penalties_home")
        pen_a = features.get("penalties_away")
        pp_h = features.get("powerplay_home")
        pp_a = features.get("powerplay_away")
        total_line = features.get("total_line")

        signals: Dict[str, Any] = {
            "tempo_signal": None,
            "tempo_ratio": None,
            "market_signal": None,
            "over_signal": False,
            "freeze_signal": False,
            "risk_level": "medium",
            "notes": [],
        }

        notes = []

        # --- Темп (SOG) ---
        if sog_h is not None and sog_a is not None and (sog_h + sog_a) > 0:
            ratio = round(sog_h / max(1, sog_a), 2)
            signals["tempo_ratio"] = ratio

            if ratio >= 1.7:
                signals["tempo_signal"] = f"Доминирование хозяев по броскам (x{ratio})"
                notes.append(f"Хозяева бросают в {ratio}x чаще — явный контроль матча")
            elif ratio >= 1.3:
                signals["tempo_signal"] = f"Преимущество хозяев по броскам (x{ratio})"
                notes.append(f"Хозяева ведут по броскам x{ratio}")
            elif ratio <= 0.6:
                signals["tempo_signal"] = f"Доминирование гостей по броскам (x{round(1/ratio,2)})"
                notes.append(f"Гости бросают в {round(1/ratio,2)}x чаще — давление гостей")
            elif ratio <= 0.77:
                signals["tempo_signal"] = f"Преимущество гостей по броскам (x{round(1/ratio,2)})"
                notes.append(f"Гости ведут по броскам x{round(1/ratio,2)}")
            else:
                signals["tempo_signal"] = f"Равный темп бросков ({sog_h}–{sog_a})"

        # --- Перекос рынка ---
        if ml_h is not None and ml_a is not None and sog_h is not None and sog_a is not None:
            sog_ratio = sog_h / max(1, sog_a)
            ml_ratio = ml_a / max(0.01, ml_h)  # высокий → хозяин фаворит по odds
            # Хозяин доминирует по SOG, но рынок не сдвинулся в его пользу
            if sog_ratio >= 1.5 and ml_ratio <= 1.05:
                signals["market_signal"] = "Перекос: хозяин давит, рынок не реагирует → возможный value"
                notes.append("Рынок недооценивает давление хозяев")
            elif sog_ratio <= 0.67 and ml_ratio >= 1.3:
                signals["market_signal"] = "Перекос: гости давят, рынок не реагирует → возможный value"
                notes.append("Рынок недооценивает давление гостей")

        # --- Over сигнал ---
        over_factors = 0
        sog_total = (sog_h or 0) + (sog_a or 0)
        pp_total = (pp_h or 0) + (pp_a or 0)

        if sog_total >= 25:
            over_factors += 1
        if pp_total >= 4:
            over_factors += 1
            notes.append(f"Много большинств ({pp_total}) — повышен риск голов")
        if period and period <= 2 and sog_total >= 20:
            over_factors += 1
        if total_line is not None and score_h is not None and score_a is not None:
            current_goals = score_h + score_a
            if period and period >= 2 and current_goals >= total_line * 0.6:
                over_factors += 1

        if over_factors >= 2:
            signals["over_signal"] = True
            notes.append("Высокий темп → предпосылки для роста тотала")

        # --- Freeze сигнал (контроль темпа / under-drift) ---
        if (
            period == 3
            and score_h is not None and score_a is not None
            and abs(score_h - score_a) >= 2
            and sog_total is not None and sog_total < 18
        ):
            signals["freeze_signal"] = True
            notes.append("3 период, лидер ведёт 2+, темп низкий → вероятен контроль / under")

        # --- Уровень риска ---
        if signals["over_signal"] and signals["freeze_signal"]:
            signals["risk_level"] = "high"
        elif signals["over_signal"] or signals["freeze_signal"]:
            signals["risk_level"] = "medium"
        elif signals["market_signal"]:
            signals["risk_level"] = "medium"
        else:
            signals["risk_level"] = "low"

        signals["notes"] = notes
        return signals

    except Exception:
        logger.exception("build_live_signals failed")
        return {
            "tempo_signal": None,
            "tempo_ratio": None,
            "market_signal": None,
            "over_signal": False,
            "freeze_signal": False,
            "risk_level": "unknown",
            "notes": [],
        }


# ---------------------------------------------------------------------------
# Форматтер для промпта
# ---------------------------------------------------------------------------

def format_features_for_prompt(
    features: Dict[str, Any],
    signals: Dict[str, Any],
    match_meta: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Форматирует features + signals в читаемый текст для LLM-промпта.
    """
    match_meta = match_meta or {}
    lines = []

    # Состояние матча
    state_parts = []
    if features.get("period"):
        state_parts.append(f"Период: {features['period']}")
    if features.get("clock_min") is not None:
        state_parts.append(f"Минута: {features['clock_min']}")
    sh = features.get("score_home")
    sa = features.get("score_away")
    if sh is not None and sa is not None:
        state_parts.append(f"Счёт: {sh}:{sa}")
    elif match_meta.get("score"):
        state_parts.append(f"Счёт: {match_meta['score']}")

    if state_parts:
        lines.append("Состояние: " + " | ".join(state_parts))

    # Броски
    if features.get("sog_home") is not None and features.get("sog_away") is not None:
        lines.append(f"Броски (SOG): хозяева {features['sog_home']} — гости {features['sog_away']}")
    else:
        lines.append("Броски (SOG): нет данных")

    # Штрафы / большинство
    pen_h = features.get("penalties_home")
    pen_a = features.get("penalties_away")
    if pen_h is not None and pen_a is not None:
        lines.append(f"Штрафные минуты: хозяева {pen_h} — гости {pen_a}")

    pp_h = features.get("powerplay_home")
    pp_a = features.get("powerplay_away")
    if pp_h is not None and pp_a is not None:
        lines.append(f"Большинство розыгрышей: хозяева {pp_h} — гости {pp_a}")

    # Коэффициенты
    odds_parts = []
    if features.get("moneyline_home") is not None:
        odds_parts.append(f"П1: {features['moneyline_home']}")
    if features.get("moneyline_away") is not None:
        odds_parts.append(f"П2: {features['moneyline_away']}")
    if features.get("total_line") is not None:
        tl = features["total_line"]
        ov = features.get("total_over") or "—"
        un = features.get("total_under") or "—"
        odds_parts.append(f"Тотал {tl} (Б:{ov} / М:{un})")
    if odds_parts:
        lines.append("Коэффициенты: " + " | ".join(odds_parts))
    else:
        lines.append("Коэффициенты: нет данных")

    lines.append("")

    # Сигналы
    lines.append("Вычисленные сигналы:")
    if signals.get("tempo_signal"):
        lines.append(f"  Темп: {signals['tempo_signal']}")
    if signals.get("market_signal"):
        lines.append(f"  Рынок: {signals['market_signal']}")
    if signals.get("over_signal"):
        lines.append("  Over-сигнал: есть предпосылки для роста тотала")
    if signals.get("freeze_signal"):
        lines.append("  Freeze-сигнал: вероятен контроль темпа / under-drift")
    lines.append(f"  Уровень риска: {signals.get('risk_level', 'medium')}")

    if signals.get("notes"):
        lines.append("  Заметки: " + "; ".join(signals["notes"]))

    has_data = (
        features.get("sog_home") is not None
        or features.get("period") is not None
        or features.get("moneyline_home") is not None
    )
    lines.append(f"\nДанных достаточно: {'да' if has_data else 'нет — используй только переданный контекст'}")

    return "\n".join(lines)
