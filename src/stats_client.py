# src/stats_client.py
"""
api-sports.io client (Hockey API v1 + Football API v3).
https://api-sports.io/documentation/hockey/v1

Env: API_SPORTS_KEY
Free tier: 100 requests/day.
"""
from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any, Dict, List, Optional

import httpx

from .data_collector import H2HData, LiveStats, TeamForm

logger = logging.getLogger(__name__)

API_KEY = os.getenv("API_SPORTS_KEY", "")
TIMEOUT = 12.0

HOCKEY_BASE = "https://v1.hockey.api-sports.io"
FOOTBALL_BASE = "https://v3.football.api-sports.io"

# League IDs for hockey
HOCKEY_LEAGUES = {
    "khl": 50,
    "nhl": 57,
    "shl": 56,
    "liiga": 51,
    "extraliga": 52,
    "vhl": 48,
}

SEASON = 2025  # Current season


def _headers() -> Dict[str, str]:
    return {"x-apisports-key": API_KEY}


async def _hockey_get(path: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Make GET request to hockey api-sports."""
    if not API_KEY:
        logger.debug("API_SPORTS_KEY not set, skipping stats fetch")
        return []

    url = f"{HOCKEY_BASE}{path}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(url, headers=_headers(), params=params)
        resp.raise_for_status()
        data = resp.json()
        return data.get("response") or []


# ---------------------------------------------------------------------------
# Team search
# ---------------------------------------------------------------------------

async def search_team_id(
    team_name: str,
    sport_slug: str = "ice-hockey",
) -> Optional[int]:
    """Search for team ID by name."""
    if sport_slug not in ("ice-hockey", "hockey"):
        return None  # Only hockey for now

    try:
        results = await _hockey_get("/teams", {"search": team_name})
        if results:
            return results[0].get("id")

        # Try shorter name (first word)
        short = team_name.split()[0] if team_name else ""
        if short and len(short) >= 3:
            results = await _hockey_get("/teams", {"search": short})
            if results:
                return results[0].get("id")
    except Exception:
        logger.exception("search_team_id failed for %s", team_name)

    return None


# ---------------------------------------------------------------------------
# Team form / statistics
# ---------------------------------------------------------------------------

async def get_team_form(
    team_id: int,
    sport_slug: str = "ice-hockey",
    league_id: Optional[int] = None,
) -> Optional[TeamForm]:
    """Fetch team season statistics and derive form."""
    if sport_slug not in ("ice-hockey", "hockey"):
        return None

    if league_id is None:
        league_id = HOCKEY_LEAGUES.get("khl", 50)

    try:
        data = await _hockey_get("/teams/statistics", {
            "team": team_id,
            "league": league_id,
            "season": SEASON,
        })
        if not data:
            return None

        stats = data[0] if isinstance(data, list) else data
        return _parse_team_form(stats)
    except Exception:
        logger.exception("get_team_form failed for team=%d", team_id)
        return None


def _parse_team_form(stats: Dict[str, Any]) -> TeamForm:
    """Parse api-sports team statistics into TeamForm."""
    form = TeamForm()

    games = stats.get("games") or {}
    wins_data = games.get("wins") or {}
    loses_data = games.get("loses") or {}

    # Total wins/losses
    form.wins = _safe_int(wins_data.get("total"))
    form.losses = _safe_int(loses_data.get("total"))

    # Home / away splits
    home_w = _safe_int(wins_data.get("home"))
    home_l = _safe_int(loses_data.get("home"))
    away_w = _safe_int(wins_data.get("away"))
    away_l = _safe_int(loses_data.get("away"))

    if home_w or home_l:
        form.home_record = f"{home_w}W-{home_l}L"
    if away_w or away_l:
        form.away_record = f"{away_w}W-{away_l}L"

    # Goals
    goals = stats.get("goals") or {}
    goals_for = goals.get("for") or {}
    goals_against = goals.get("against") or {}
    total_games = form.wins + form.losses or 1
    form.goals_per_game = round(_safe_float(goals_for.get("total")) / total_games, 1)
    form.goals_against_per_game = round(_safe_float(goals_against.get("total")) / total_games, 1)

    # Build last_10 string
    form.last_10 = f"{form.wins}W-{form.losses}L"

    return form


# ---------------------------------------------------------------------------
# H2H
# ---------------------------------------------------------------------------

async def get_h2h(
    team1_id: int,
    team2_id: int,
    last: int = 10,
) -> Optional[H2HData]:
    """Fetch head-to-head history."""
    try:
        data = await _hockey_get("/games/h2h", {
            "h2h": f"{team1_id}-{team2_id}",
            "last": last,
        })
        if not data:
            return None

        return _parse_h2h(data, team1_id, team2_id)
    except Exception:
        logger.exception("get_h2h failed for %d vs %d", team1_id, team2_id)
        return None


def _parse_h2h(games: List[Dict[str, Any]], home_id: int, away_id: int) -> H2HData:
    """Parse H2H games list into H2HData."""
    h2h = H2HData(total_games=len(games))
    total_goals = 0

    for game in games:
        teams = game.get("teams") or {}
        scores = game.get("scores") or {}

        home_team = teams.get("home") or {}
        away_team = teams.get("away") or {}
        h_score = _safe_int(scores.get("home"))
        a_score = _safe_int(scores.get("away"))

        total_goals += h_score + a_score

        game_home_id = home_team.get("id")
        if h_score > a_score:
            if game_home_id == home_id:
                h2h.home_wins += 1
            else:
                h2h.away_wins += 1
        elif a_score > h_score:
            if game_home_id == home_id:
                h2h.away_wins += 1
            else:
                h2h.home_wins += 1
        else:
            h2h.draws += 1

    if h2h.total_games > 0:
        h2h.avg_total = round(total_goals / h2h.total_games, 1)

    # Last result
    if games:
        last = games[0]
        lt = last.get("teams") or {}
        ls = last.get("scores") or {}
        h_name = (lt.get("home") or {}).get("name", "?")
        h_s = _safe_int(ls.get("home"))
        a_s = _safe_int(ls.get("away"))
        h2h.last_result = f"{h_name} {h_s}:{a_s}"

    return h2h


# ---------------------------------------------------------------------------
# Live stats
# ---------------------------------------------------------------------------

async def get_live_stats(
    match_id: str,
    sport_slug: str = "ice-hockey",
) -> Optional[LiveStats]:
    """Fetch live game statistics."""
    if sport_slug not in ("ice-hockey", "hockey"):
        return None

    try:
        game_id = int(match_id)
    except (ValueError, TypeError):
        return None

    try:
        data = await _hockey_get("/games/statistics", {"id": game_id})
        if not data:
            return None

        return _parse_live_stats(data)
    except Exception:
        logger.exception("get_live_stats failed for %s", match_id)
        return None


def _parse_live_stats(data: List[Dict[str, Any]]) -> LiveStats:
    """Parse api-sports game statistics into LiveStats."""
    stats = LiveStats()

    for team_data in data:
        team = team_data.get("team") or {}
        team_stats = team_data.get("statistics") or {}

        # Determine home/away by position in list (first = home)
        is_home = data.index(team_data) == 0

        shots = team_stats.get("shots_on_goal") or {}
        shots_total = _safe_int(shots.get("total") if isinstance(shots, dict) else shots)

        penalties = _safe_int(
            (team_stats.get("penalty_minutes") or {}).get("total")
            if isinstance(team_stats.get("penalty_minutes"), dict)
            else team_stats.get("penalty_minutes")
        )

        faceoffs = team_stats.get("faceoffs_won") or {}
        faceoffs_pct = _safe_float(
            faceoffs.get("percentage", "0").replace("%", "")
            if isinstance(faceoffs, dict) else 0
        )

        pp = team_stats.get("power_play") or {}
        pp_total = _safe_int(pp.get("total") if isinstance(pp, dict) else 0)
        pp_scored = _safe_int(pp.get("scored") if isinstance(pp, dict) else 0)
        pp_str = f"{pp_scored}/{pp_total}" if pp_total else ""

        hits = _safe_int(
            (team_stats.get("hits") or {}).get("total")
            if isinstance(team_stats.get("hits"), dict)
            else team_stats.get("hits")
        )

        blocked = _safe_int(
            (team_stats.get("blocked_shots") or {}).get("total")
            if isinstance(team_stats.get("blocked_shots"), dict)
            else team_stats.get("blocked_shots")
        )

        if is_home:
            stats.shots_home = shots_total
            stats.penalties_home = penalties
            stats.faceoffs_home = faceoffs_pct
            stats.powerplay_home = pp_str
            stats.hits_home = hits
            stats.blocked_home = blocked
        else:
            stats.shots_away = shots_total
            stats.penalties_away = penalties
            stats.faceoffs_away = faceoffs_pct
            stats.powerplay_away = pp_str
            stats.hits_away = hits
            stats.blocked_away = blocked

    return stats


# ---------------------------------------------------------------------------
# Games today
# ---------------------------------------------------------------------------

async def get_games_today(
    league_id: int = 50,
    sport_slug: str = "ice-hockey",
) -> List[Dict[str, Any]]:
    """Fetch all games for today."""
    if sport_slug not in ("ice-hockey", "hockey"):
        return []

    try:
        today = date.today().isoformat()
        return await _hockey_get("/games", {
            "league": league_id,
            "season": SEASON,
            "date": today,
        })
    except Exception:
        logger.exception("get_games_today failed")
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_int(val: Any) -> int:
    if val is None:
        return 0
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


def _safe_float(val: Any) -> float:
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0
