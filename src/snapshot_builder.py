# src/snapshot_builder.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


def safe_get(d: Dict[str, Any], *keys: str, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def build_snapshot(
    *,
    sport: str,
    match: Dict[str, Any],
    odds: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Нормализуем матч в единый формат.
    match — то, что ты уже получаешь из API (SportAPI).
    odds/extra — опционально (если есть).
    """
    home = safe_get(match, "team_a", default=safe_get(match, "homeTeam", "name", default=""))
    away = safe_get(match, "team_b", default=safe_get(match, "awayTeam", "name", default=""))

    league = safe_get(match, "league", default=safe_get(match, "tournament", "name", default=""))
    country = safe_get(match, "country", default=safe_get(match, "tournament", "country", default=""))

    status = (
        safe_get(match, "status", default=safe_get(match, "match_status", default=""))
        or ""
    )

    score = safe_get(match, "score", default="")
    if isinstance(score, dict):
        score = f"{score.get('home', '')}:{score.get('away', '')}".strip(":")

    snapshot: Dict[str, Any] = {
        "sport": sport,
        "league": league,
        "country": country,
        "home_team": home,
        "away_team": away,
        "status_raw": status,
        "score": score,
        "time": safe_get(match, "time", default=safe_get(match, "date", default="")),
        "period": safe_get(match, "period", default=""),
        "stats": {
            "shots": safe_get(match, "shots", default=None),
            "penalties": safe_get(match, "penalties", default=None),
            "faceoffs": safe_get(match, "faceoffs", default=None),
            "xg": safe_get(match, "xg", default=None),
        },
        "odds": odds or {},
        "extra": extra or {},
    }

    # очищаем None чтобы промт был чище
    def _clean(obj):
        if isinstance(obj, dict):
            return {k: _clean(v) for k, v in obj.items() if v is not None and v != ""}
        if isinstance(obj, list):
            return [_clean(x) for x in obj if x is not None and x != ""]
        return obj

    return _clean(snapshot)
