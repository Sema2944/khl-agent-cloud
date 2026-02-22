# src/pro_engine/snapshot.py
"""
MatchLiveSnapshot normalizer.
raw (SportAPI) + odds_raw + match_meta → unified snapshot dict.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers (safe coercion)
# ---------------------------------------------------------------------------

def _i(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(float(str(v).strip()))
    except Exception:
        return None


def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return round(float(str(v).strip()), 3)
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
# Main builder
# ---------------------------------------------------------------------------

def build_snapshot(
    raw: Dict[str, Any],
    odds_raw: Dict[str, Any],
    match_meta: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Builds a normalized MatchLiveSnapshot dict.
    Never raises — returns empty snapshot on error.
    """
    try:
        raw = raw or {}
        odds_raw = odds_raw or {}
        match_meta = match_meta or {}

        home = str(
            match_meta.get("home")
            or _dig(raw, "homeTeam", "name")
            or _dig(raw, "teams", "home", "name")
            or "Хозяева"
        ).strip()
        away = str(
            match_meta.get("away")
            or _dig(raw, "awayTeam", "name")
            or _dig(raw, "teams", "away", "name")
            or "Гости"
        ).strip()

        # Extract title → home / away if not set
        if home == "Хозяева" or away == "Гости":
            title = str(match_meta.get("title") or "").strip()
            if " — " in title:
                parts = title.split(" — ", 1)
                if home == "Хозяева":
                    home = parts[0].strip()
                if away == "Гости":
                    away = parts[1].strip()

        clock = _build_clock(raw)
        score = _build_score(raw, match_meta)
        stats = _build_stats(raw)
        odds = _build_odds(odds_raw, raw)
        events = _extract_events(raw, home, away)

        # Provider keys for debug
        keys_present = list(raw.keys())[:30] if isinstance(raw, dict) else []

        snapshot = {
            "match_id": str(match_meta.get("match_id") or ""),
            "sport_slug": str(match_meta.get("sport") or "ice-hockey"),
            "league": str(match_meta.get("league") or ""),
            "country": str(match_meta.get("country") or ""),
            "teams": {"home": home, "away": away},
            "status": str(match_meta.get("status") or raw.get("status") or ""),
            "clock": clock,
            "score": score,
            "stats": stats,
            "odds": odds,
            "events": events,
            "meta": {
                "provider_keys_present": keys_present,
                "fetched_at_ts": int(time.time()),
            },
        }

        logger.info(
            "PRO snapshot: match=%s P%s %s' %s:%s shots=%s:%s ml=%.2f/%.2f",
            snapshot["match_id"],
            clock.get("period") or "?",
            clock.get("minute") or "?",
            score.get("home"),
            score.get("away"),
            stats.get("shots_home"),
            stats.get("shots_away"),
            odds.get("ml_home") or 0,
            odds.get("ml_away") or 0,
        )

        return snapshot

    except Exception:
        logger.exception("build_snapshot failed")
        return _empty_snapshot(match_meta or {})


# ---------------------------------------------------------------------------
# Sub-builders
# ---------------------------------------------------------------------------

def _build_clock(raw: Dict[str, Any]) -> Dict[str, Any]:
    period = _i(_first(
        raw.get("period"), raw.get("currentPeriod"), raw.get("current_period"),
        _dig(raw, "time", "period"), _dig(raw, "clock", "period"),
        _dig(raw, "status", "period"),
    ))
    minute = _i(_first(
        raw.get("minute"), raw.get("clock"),
        _dig(raw, "time", "minute"), _dig(raw, "time", "clock"),
        _dig(raw, "clock", "minute"), _dig(raw, "clock", "current"),
    ))
    second = _i(_first(
        _dig(raw, "clock", "second"), _dig(raw, "time", "second"),
    ))
    return {"period": period, "minute": minute, "second": second, "raw": raw.get("clock") or raw.get("time")}


def _build_score(raw: Dict[str, Any], match_meta: Dict[str, Any]) -> Dict[str, Any]:
    score_h = _score_side(raw, "home")
    score_a = _score_side(raw, "away")

    if score_h is None and score_a is None:
        ms = str(match_meta.get("score") or "").strip()
        if ":" in ms:
            parts = ms.split(":")
            try:
                score_h = int(parts[0].strip())
                score_a = int(parts[1].strip().split()[0])
            except Exception:
                pass

    return {
        "home": score_h,
        "away": score_a,
        "raw": str(match_meta.get("score") or ""),
    }


def _score_side(raw: Dict[str, Any], side: str) -> Optional[int]:
    candidates = [
        raw.get(f"{side}Score"), raw.get(f"score_{side}"),
        raw.get(f"goals{side.capitalize()}"),
        _dig(raw, "score", side), _dig(raw, "scores", side),
        _dig(raw, "result", side),
        _dig(raw, f"{side}Team", "score"), _dig(raw, f"{side}Team", "goals"),
        _dig(raw, "teams", side, "score"), _dig(raw, "teams", side, "goals"),
    ]
    for c in candidates:
        v = _i(c)
        if v is not None:
            return v
    return None


def _build_stats(raw: Dict[str, Any]) -> Dict[str, Any]:
    shots_h, shots_a = _extract_shots(raw)
    sog_h, sog_a = _extract_sog(raw, shots_h, shots_a)
    pen_h, pen_a = _extract_penalties(raw)
    pp_h, pp_a = _extract_powerplay(raw)
    xg_h, xg_a = _extract_xg(raw)
    poss_h, poss_a = _extract_possession(raw)
    da_h, da_a = _extract_dangerous(raw)
    corners_h, corners_a = _extract_corners(raw)

    return {
        "shots_home": shots_h,
        "shots_away": shots_a,
        "shots_on_goal_home": sog_h,
        "shots_on_goal_away": sog_a,
        "penalties_home": pen_h,
        "penalties_away": pen_a,
        "pp_home": pp_h,
        "pp_away": pp_a,
        "xg_home": xg_h,
        "xg_away": xg_a,
        "possession_home": poss_h,
        "possession_away": poss_a,
        "dangerous_attacks_home": da_h,
        "dangerous_attacks_away": da_a,
        "corners_home": corners_h,
        "corners_away": corners_a,
    }


def _stat_from_list(stats_list: list, *type_keywords: str) -> tuple:
    for item in stats_list:
        if not isinstance(item, dict):
            continue
        t = str(item.get("type") or item.get("name") or "").lower()
        if any(kw in t for kw in type_keywords):
            h = _i(_first(item.get("home"), item.get("homeTeam"), item.get("h"), item.get("value_home")))
            a = _i(_first(item.get("away"), item.get("awayTeam"), item.get("a"), item.get("value_away")))
            if h is not None or a is not None:
                return h, a
    return None, None


def _extract_shots(raw: Dict[str, Any]) -> tuple:
    for kh, ka in [("shots_home", "shots_away"), ("shotsHome", "shotsAway"), ("shotsTotal_home", "shotsTotal_away")]:
        h, a = _i(raw.get(kh)), _i(raw.get(ka))
        if h is not None and a is not None:
            return h, a

    for sk in ("statistics", "stats"):
        stats = raw.get(sk)
        if isinstance(stats, dict):
            for key in ("shots", "shotsTotal", "totalShots"):
                obj = stats.get(key)
                if isinstance(obj, dict):
                    h = _i(_first(obj.get("home"), obj.get("h")))
                    a = _i(_first(obj.get("away"), obj.get("a")))
                    if h is not None and a is not None:
                        return h, a
        if isinstance(stats, list):
            h, a = _stat_from_list(stats, "shot", "sog")
            if h is not None and a is not None:
                return h, a

    return None, None


def _extract_sog(raw: Dict[str, Any], fallback_h=None, fallback_a=None) -> tuple:
    for kh, ka in [("shotsOnGoal_home", "shotsOnGoal_away"), ("sog_home", "sog_away"), ("shotsOnTarget_home", "shotsOnTarget_away")]:
        h, a = _i(raw.get(kh)), _i(raw.get(ka))
        if h is not None and a is not None:
            return h, a

    for sk in ("statistics", "stats"):
        stats = raw.get(sk)
        if isinstance(stats, dict):
            for key in ("shotsOnGoal", "shotsOnTarget", "sog"):
                obj = stats.get(key)
                if isinstance(obj, dict):
                    h = _i(_first(obj.get("home"), obj.get("h")))
                    a = _i(_first(obj.get("away"), obj.get("a")))
                    if h is not None and a is not None:
                        return h, a
        if isinstance(stats, list):
            h, a = _stat_from_list(stats, "on goal", "on target", "sog")
            if h is not None and a is not None:
                return h, a

    return fallback_h, fallback_a  # fall back to total shots


def _extract_penalties(raw: Dict[str, Any]) -> tuple:
    for kh, ka in [("penaltyMinutes_home", "penaltyMinutes_away"), ("penalties_home", "penalties_away"), ("penMin_home", "penMin_away")]:
        h, a = _i(raw.get(kh)), _i(raw.get(ka))
        if h is not None and a is not None:
            return h, a

    for sk in ("statistics", "stats", "penalties"):
        stats = raw.get(sk)
        if isinstance(stats, list):
            h, a = _stat_from_list(stats, "penalt", "pim")
            if h is not None and a is not None:
                return h, a

    return None, None


def _extract_powerplay(raw: Dict[str, Any]) -> tuple:
    for kh, ka in [("powerplay_home", "powerplay_away"), ("pp_home", "pp_away"), ("powerPlays_home", "powerPlays_away")]:
        h, a = _i(raw.get(kh)), _i(raw.get(ka))
        if h is not None and a is not None:
            return h, a

    for sk in ("statistics", "stats"):
        stats = raw.get(sk)
        if isinstance(stats, list):
            h, a = _stat_from_list(stats, "power play", "powerplay", "pp ")
            if h is not None and a is not None:
                return h, a

    return None, None


def _extract_xg(raw: Dict[str, Any]) -> tuple:
    for kh, ka in [("xg_home", "xg_away"), ("expectedGoals_home", "expectedGoals_away")]:
        h = _f(raw.get(kh))
        a = _f(raw.get(ka))
        if h is not None and a is not None:
            return h, a
    return None, None


def _extract_possession(raw: Dict[str, Any]) -> tuple:
    for kh, ka in [("possession_home", "possession_away"), ("ballPossession_home", "ballPossession_away")]:
        h = _f(raw.get(kh))
        a = _f(raw.get(ka))
        if h is not None and a is not None:
            return h, a

    for sk in ("statistics", "stats"):
        stats = raw.get(sk)
        if isinstance(stats, list):
            h, a = _stat_from_list(stats, "possession")
            if h is not None and a is not None:
                return _f(h), _f(a)

    return None, None


def _extract_dangerous(raw: Dict[str, Any]) -> tuple:
    for sk in ("statistics", "stats"):
        stats = raw.get(sk)
        if isinstance(stats, list):
            h, a = _stat_from_list(stats, "dangerous", "attack")
            if h is not None and a is not None:
                return h, a
    return None, None


def _extract_corners(raw: Dict[str, Any]) -> tuple:
    for kh, ka in [("corners_home", "corners_away"), ("cornerKicks_home", "cornerKicks_away")]:
        h, a = _i(raw.get(kh)), _i(raw.get(ka))
        if h is not None and a is not None:
            return h, a

    for sk in ("statistics", "stats"):
        stats = raw.get(sk)
        if isinstance(stats, list):
            h, a = _stat_from_list(stats, "corner")
            if h is not None and a is not None:
                return h, a

    return None, None


def _build_odds(odds_raw: Dict[str, Any], raw: Dict[str, Any]) -> Dict[str, Any]:
    ml_h = ml_a = draw = total_line = total_over = total_under = hcp_line_h = hcp_odds_h = hcp_line_a = hcp_odds_a = None

    src = odds_raw if odds_raw else raw
    markets = src.get("markets") if isinstance(src, dict) else None

    if not isinstance(markets, list):
        ml_h = _f(_first(src.get("moneyline_home"), src.get("homeOdds"), src.get("1")))
        ml_a = _f(_first(src.get("moneyline_away"), src.get("awayOdds"), src.get("2")))
        draw = _f(_first(src.get("draw"), src.get("X"), src.get("drawOdds")))
        total_line = _f(_first(src.get("total_line"), src.get("total"), src.get("ou_line")))
        total_over = _f(_first(src.get("total_over"), src.get("over")))
        total_under = _f(_first(src.get("total_under"), src.get("under")))
    else:
        for market in markets:
            if not isinstance(market, dict):
                continue
            name = str(market.get("name") or market.get("marketId") or "").lower()
            choices = market.get("choices") or []
            if not isinstance(choices, list):
                continue

            if any(x in name for x in ("moneyline", "1x2", "match winner", "result", "winner")):
                for c in choices:
                    if not isinstance(c, dict):
                        continue
                    cn = str(c.get("name") or "").lower()
                    odd = _f(c.get("odd") or c.get("odds") or c.get("price"))
                    if any(x in cn for x in ("home", "1", "хозяев", "п1")):
                        ml_h = odd
                    elif any(x in cn for x in ("away", "2", "гостей", "п2")):
                        ml_a = odd
                    elif any(x in cn for x in ("draw", "x", "ничья")):
                        draw = odd

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

    return {
        "ml_home": ml_h,
        "ml_away": ml_a,
        "draw": draw,
        "total_line": total_line,
        "total_over": total_over,
        "total_under": total_under,
        "hcp_line_home": hcp_line_h,
        "hcp_odds_home": hcp_odds_h,
        "hcp_line_away": hcp_line_a,
        "hcp_odds_away": hcp_odds_a,
    }


def _empty_snapshot(match_meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "match_id": str(match_meta.get("match_id") or ""),
        "sport_slug": str(match_meta.get("sport") or "ice-hockey"),
        "league": str(match_meta.get("league") or ""),
        "country": str(match_meta.get("country") or ""),
        "teams": {"home": "Хозяева", "away": "Гости"},
        "status": "",
        "clock": {"period": None, "minute": None, "second": None, "raw": None},
        "score": {"home": None, "away": None, "raw": ""},
        "stats": {k: None for k in ("shots_home", "shots_away", "shots_on_goal_home", "shots_on_goal_away",
                                    "penalties_home", "penalties_away", "pp_home", "pp_away",
                                    "xg_home", "xg_away", "possession_home", "possession_away",
                                    "dangerous_attacks_home", "dangerous_attacks_away", "corners_home", "corners_away")},
        "odds": {k: None for k in ("ml_home", "ml_away", "draw", "total_line", "total_over", "total_under",
                                   "hcp_line_home", "hcp_odds_home", "hcp_line_away", "hcp_odds_away")},
        "events": [],
        "meta": {"provider_keys_present": [], "fetched_at_ts": int(time.time())},
    }


# ---------------------------------------------------------------------------
# Events extraction
# ---------------------------------------------------------------------------

def _extract_events(raw: Dict[str, Any], home_name: str = "", away_name: str = "") -> list:
    """
    Parse events/incidents from raw API data.
    Returns list of formatted event strings (most recent first), max 8.
    """
    _SKIP_EVENT_TYPES = {"period", "period_start", "period_end", "whistle",
                          "periodstart", "periodend", "half", "halfstart", "halfend"}

    try:
        events_raw = raw.get("events") or raw.get("incidents") or []
        if not isinstance(events_raw, list):
            return []

        parsed = []
        for ev in events_raw:
            if not isinstance(ev, dict):
                continue

            # Extract minute
            minute = _i(
                ev.get("minute") or ev.get("time") or ev.get("matchTime")
                or _dig(ev, "time", "minute") or ev.get("clock")
            )

            # Extract event type
            ev_type = str(
                ev.get("type") or ev.get("incidentType") or ev.get("eventType")
                or ev.get("name") or ""
            ).strip().lower()

            # Skip period/whistle noise events
            if ev_type.replace("_", "").replace("-", "") in _SKIP_EVENT_TYPES:
                continue

            # Extract team info
            team_raw = str(
                ev.get("team") or ev.get("teamName")
                or _dig(ev, "team", "name") or ""
            ).strip()
            # Resolve home/away labels
            team_side = str(ev.get("teamSide") or ev.get("side") or "").lower()
            if not team_raw and team_side:
                if "home" in team_side:
                    team_raw = home_name or "Хозяева"
                elif "away" in team_side:
                    team_raw = away_name or "Гости"
            # Replace literal "home"/"away" with real team names
            if team_raw.lower() == "home":
                team_raw = home_name or "Хозяева"
            elif team_raw.lower() == "away":
                team_raw = away_name or "Гости"

            # Extract player
            player = str(
                ev.get("player") or ev.get("playerName")
                or _dig(ev, "player", "name") or _dig(ev, "player", "shortName")
                or ""
            ).strip()

            # Extract detail (penalty minutes, assist, etc.)
            detail = str(
                ev.get("detail") or ev.get("description") or ev.get("comment")
                or ev.get("reason") or ""
            ).strip()

            # Format event line
            emoji = _event_emoji(ev_type)
            type_label = _event_label(ev_type)

            parts = []
            if minute is not None:
                parts.append(f"{minute}'")
            parts.append(f"{emoji} {type_label}")
            if team_raw:
                parts.append(f"— {team_raw}")
            if player:
                parts.append(f"({player})")
            if detail:
                parts.append(f"[{detail[:40]}]")

            line = " ".join(parts)
            parsed.append({"minute": minute or 0, "line": line})

        # Sort by minute descending (most recent first)
        parsed.sort(key=lambda x: x["minute"], reverse=True)
        return [p["line"] for p in parsed[:8]]

    except Exception:
        logger.exception("_extract_events failed")
        return []


def _event_emoji(ev_type: str) -> str:
    t = (ev_type or "").lower()
    if "goal" in t or "гол" in t:
        return "🏒"
    if "penalt" in t or "удален" in t or "штраф" in t:
        return "🟡"
    if "card" in t:
        if "red" in t:
            return "🟥"
        return "🟨"
    if "subst" in t or "замен" in t:
        return "🔄"
    if "shot" in t or "бросок" in t:
        return "🏒"
    if "period" in t or "half" in t or "quarter" in t:
        return "⏱"
    return "▪️"


def _event_label(ev_type: str) -> str:
    t = (ev_type or "").lower()
    if "goal" in t or "гол" in t:
        return "Гол"
    if "penalt" in t or "удален" in t:
        return "Удаление"
    if "yellow" in t or "жёлт" in t:
        return "ЖК"
    if "red" in t or "красн" in t:
        return "КК"
    if "subst" in t or "замен" in t:
        return "Замена"
    if "shot" in t or "бросок" in t:
        return "Бросок"
    if "period" in t:
        return "Период"
    return ev_type.capitalize()[:20] if ev_type else "Событие"
