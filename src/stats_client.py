# src/stats_client.py
"""
Universal api-sports.io client — works for all sports via sports_config.
https://api-sports.io/documentation

Env: API_SPORTS_KEY
Pro tier ($19/mo): all sports, one key.
"""
from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any, Dict, List, Optional

import httpx

from .data_collector import H2HData, LiveStats, TeamForm
from .sports_config import (
    get_sport_config,
    get_api_base,
    get_endpoints,
    get_leagues,
    get_match_param,
    resolve_sport_slug,
)

logger = logging.getLogger(__name__)

API_KEY = os.getenv("API_SPORTS_KEY", "")
TIMEOUT = 12.0


def _headers() -> Dict[str, str]:
    return {"x-apisports-key": API_KEY}


# ---------------------------------------------------------------------------
# Universal API request
# ---------------------------------------------------------------------------

async def _api_get(
    sport_slug: str,
    path: str,
    params: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Universal GET request to api-sports.io for any sport."""
    if not API_KEY:
        logger.debug("API_SPORTS_KEY not set, skipping stats fetch")
        return []

    base = get_api_base(sport_slug)
    if not base:
        logger.debug("No API base for sport=%s", sport_slug)
        return []

    url = f"{base}{path}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(url, headers=_headers(), params=params)
        resp.raise_for_status()
        data = resp.json()
        return data.get("response") or []


# Backward compatibility: hockey-specific shortcut
async def _hockey_get(path: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    return await _api_get("ice-hockey", path, params)


# ---------------------------------------------------------------------------
# Team search (universal) with in-memory cache
# ---------------------------------------------------------------------------

# Cache: "slug:team_name" → team_id (persists for process lifetime)
_team_id_cache: Dict[str, Optional[int]] = {}


async def resolve_team_id(
    team_name: str,
    sport_slug: str = "ice-hockey",
) -> Optional[int]:
    """
    Resolve team name → api-sports.io team ID with caching.
    Always searches api-sports.io by name (ignores IDs from other APIs).
    """
    slug = resolve_sport_slug(sport_slug) or sport_slug
    cache_key = f"{slug}:{team_name.lower().strip()}"

    if cache_key in _team_id_cache:
        cached = _team_id_cache[cache_key]
        logger.debug("resolve_team_id CACHE HIT: '%s' → %s (%s)", team_name, cached, slug)
        return cached

    tid = await search_team_id(team_name, sport_slug)
    _team_id_cache[cache_key] = tid
    return tid


async def search_team_id(
    team_name: str,
    sport_slug: str = "ice-hockey",
) -> Optional[int]:
    """Search for team ID by name. Works for any sport with /teams endpoint."""
    slug = resolve_sport_slug(sport_slug) or sport_slug

    try:
        results = await _api_get(slug, "/teams", {"search": team_name})
        if results:
            tid = results[0].get("id")
            tname = results[0].get("name", "?")
            logger.info("search_team_id: '%s' → id=%s name='%s' (%s)", team_name, tid, tname, slug)
            return tid

        # Try shorter name (first word)
        short = team_name.split()[0] if team_name else ""
        if short and len(short) >= 3:
            results = await _api_get(slug, "/teams", {"search": short})
            if results:
                tid = results[0].get("id")
                tname = results[0].get("name", "?")
                logger.info("search_team_id: '%s' (short='%s') → id=%s name='%s' (%s)",
                            team_name, short, tid, tname, slug)
                return tid

        logger.warning("search_team_id: NOT FOUND '%s' (%s)", team_name, slug)
    except Exception:
        logger.exception("search_team_id failed for %s (%s)", team_name, slug)

    return None


# ---------------------------------------------------------------------------
# Team form / statistics (universal)
# ---------------------------------------------------------------------------

async def get_team_form(
    team_id: int,
    sport_slug: str = "ice-hockey",
    league_id: Optional[int] = None,
) -> Optional[TeamForm]:
    """Fetch team season statistics and derive form."""
    slug = resolve_sport_slug(sport_slug) or sport_slug
    cfg = get_sport_config(slug)
    if not cfg:
        logger.debug("get_team_form: no config for %s", slug)
        return None

    # Default league: first priority-1 league for this sport
    if league_id is None:
        leagues = get_leagues(slug, max_priority=1)
        if leagues:
            league_id = next(iter(leagues))
        else:
            leagues = get_leagues(slug)
            league_id = next(iter(leagues)) if leagues else None
    if league_id is None:
        logger.debug("get_team_form: no league for %s", slug)
        return None

    # Get season for this league
    league_info = cfg.get("leagues", {}).get(league_id, {})
    season = league_info.get("season", 2025)

    # Determine the right endpoint: football uses /teams/statistics, others may differ
    stats_endpoint = "/teams/statistics"

    try:
        data = await _api_get(slug, stats_endpoint, {
            "team": team_id,
            "league": league_id,
            "season": season,
        })
        if not data:
            logger.warning("get_team_form: empty response for team=%d league=%d season=%s sport=%s",
                           team_id, league_id, season, slug)
            return None

        stats = data[0] if isinstance(data, list) else data
        logger.info("get_team_form RAW keys: %s (team=%d sport=%s)",
                     list(stats.keys())[:20] if isinstance(stats, dict) else type(stats).__name__,
                     team_id, slug)
        result = _parse_team_form(stats)
        logger.info("get_team_form PARSED: wins=%d losses=%d last_10=%s home=%s away=%s (team=%d)",
                     result.wins, result.losses, result.last_10,
                     result.home_record, result.away_record, team_id)
        return result
    except Exception:
        logger.exception("get_team_form failed for team=%d sport=%s", team_id, slug)
        return None


def _parse_team_form(stats: Dict[str, Any]) -> TeamForm:
    """Parse api-sports team statistics into TeamForm (universal)."""
    form = TeamForm()

    # Unwrap nested "statistics" wrapper (hockey/basketball api-sports format)
    inner = stats
    if "statistics" in stats and isinstance(stats["statistics"], dict):
        inner = stats["statistics"]

    # Different sports return stats differently; handle common patterns
    games = inner.get("games") or inner.get("fixtures") or stats.get("games") or stats.get("fixtures") or {}
    wins_data = games.get("wins") or {}
    loses_data = games.get("loses") or games.get("losses") or {}

    # If wins_data is a dict with total/home/away
    if isinstance(wins_data, dict):
        form.wins = _safe_int(wins_data.get("total") or wins_data.get("all"))
        home_w = _safe_int(wins_data.get("home"))
        away_w = _safe_int(wins_data.get("away"))
    else:
        form.wins = _safe_int(wins_data)
        home_w = 0
        away_w = 0

    if isinstance(loses_data, dict):
        form.losses = _safe_int(loses_data.get("total") or loses_data.get("all"))
        home_l = _safe_int(loses_data.get("home"))
        away_l = _safe_int(loses_data.get("away"))
    else:
        form.losses = _safe_int(loses_data)
        home_l = 0
        away_l = 0

    if home_w or home_l:
        form.home_record = f"{home_w}W-{home_l}L"
    if away_w or away_l:
        form.away_record = f"{away_w}W-{away_l}L"

    # Goals / points — also check inside inner
    goals = inner.get("goals") or stats.get("goals") or {}
    goals_for = goals.get("for") or {}
    goals_against = goals.get("against") or {}

    total_games = form.wins + form.losses or 1

    # goals_for can be: int, {"total": int}, {"total": {"total": int}}
    if isinstance(goals_for, dict):
        gf_raw = goals_for.get("total")
        if isinstance(gf_raw, dict):
            gf = _safe_float(gf_raw.get("total") or gf_raw.get("all"))
        else:
            gf = _safe_float(gf_raw)
    else:
        gf = _safe_float(goals_for)

    if isinstance(goals_against, dict):
        ga_raw = goals_against.get("total")
        if isinstance(ga_raw, dict):
            ga = _safe_float(ga_raw.get("total") or ga_raw.get("all"))
        else:
            ga = _safe_float(ga_raw)
    else:
        ga = _safe_float(goals_against)

    form.goals_per_game = round(gf / total_games, 1)
    form.goals_against_per_game = round(ga / total_games, 1)

    # Form string
    form.last_10 = f"{form.wins}W-{form.losses}L"

    # Streak from "form" field (some sports return "WWLWW")
    form_str = inner.get("form") or stats.get("form", "")
    if form_str and isinstance(form_str, str):
        streak_char = form_str[-1] if form_str else ""
        count = 0
        for c in reversed(form_str):
            if c == streak_char:
                count += 1
            else:
                break
        if streak_char and count:
            form.streak = f"{count}{streak_char}"

    return form


# ---------------------------------------------------------------------------
# H2H (universal)
# ---------------------------------------------------------------------------

async def get_h2h(
    team1_id: int,
    team2_id: int,
    last: int = 10,
    sport_slug: str = "ice-hockey",
) -> Optional[H2HData]:
    """Fetch head-to-head history for any sport."""
    slug = resolve_sport_slug(sport_slug) or sport_slug
    endpoints = get_endpoints(slug)
    h2h_endpoint = endpoints.get("h2h", "/games/h2h")

    try:
        data = await _api_get(slug, h2h_endpoint, {
            "h2h": f"{team1_id}-{team2_id}",
            "last": last,
        })

        if not data:
            logger.warning("get_h2h: empty response for %d vs %d (%s)", team1_id, team2_id, slug)
            return None

        logger.info("get_h2h RAW: %d games returned for %d vs %d, first_keys=%s",
                     len(data), team1_id, team2_id,
                     list(data[0].keys())[:15] if data and isinstance(data[0], dict) else "?")

        result = _parse_h2h(data, team1_id, team2_id)
        logger.info("get_h2h PARSED: total=%d home_w=%d away_w=%d draws=%d avg_total=%.1f",
                     result.total_games, result.home_wins, result.away_wins,
                     result.draws, result.avg_total)
        return result
    except Exception:
        logger.exception("get_h2h failed for %d vs %d (%s)", team1_id, team2_id, slug)
        return None


def _parse_h2h(games: List[Dict[str, Any]], home_id: int, away_id: int) -> H2HData:
    """Parse H2H games list into H2HData."""
    h2h = H2HData(total_games=len(games))
    total_goals = 0

    for game in games:
        teams = game.get("teams") or {}
        scores = game.get("scores") or game.get("goals") or {}

        home_team = teams.get("home") or {}
        away_team = teams.get("away") or {}

        # Scores can be nested (football: goals.home) or flat (hockey: scores.home)
        h_score = _safe_int(
            scores.get("home") if not isinstance(scores.get("home"), dict)
            else scores.get("home", {}).get("total", 0)
        )
        a_score = _safe_int(
            scores.get("away") if not isinstance(scores.get("away"), dict)
            else scores.get("away", {}).get("total", 0)
        )

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
        last_game = games[0]
        lt = last_game.get("teams") or {}
        ls = last_game.get("scores") or last_game.get("goals") or {}
        h_name = (lt.get("home") or {}).get("name", "?")
        h_s = _safe_int(ls.get("home") if not isinstance(ls.get("home"), dict) else ls.get("home", {}).get("total", 0))
        a_s = _safe_int(ls.get("away") if not isinstance(ls.get("away"), dict) else ls.get("away", {}).get("total", 0))
        h2h.last_result = f"{h_name} {h_s}:{a_s}"

    return h2h


# ---------------------------------------------------------------------------
# Live stats (universal)
# ---------------------------------------------------------------------------

async def get_live_stats(
    match_id: str,
    sport_slug: str = "ice-hockey",
) -> Optional[LiveStats]:
    """Fetch live game statistics for any sport."""
    slug = resolve_sport_slug(sport_slug) or sport_slug
    endpoints = get_endpoints(slug)
    stats_endpoint = endpoints.get("statistics")
    if not stats_endpoint:
        return None

    try:
        game_id = int(match_id)
    except (ValueError, TypeError):
        return None

    match_param = get_match_param(slug)

    try:
        data = await _api_get(slug, stats_endpoint, {match_param: game_id})
        if not data:
            return None

        return _parse_live_stats(data)
    except Exception:
        logger.exception("get_live_stats failed for %s (%s)", match_id, slug)
        return None


def _parse_live_stats(data: List[Dict[str, Any]]) -> LiveStats:
    """Parse api-sports game statistics into LiveStats."""
    stats = LiveStats()

    for team_data in data:
        team_stats = team_data.get("statistics") or {}

        # Determine home/away by position in list (first = home)
        is_home = data.index(team_data) == 0

        # Handle both hockey and football stat formats
        shots = team_stats.get("shots_on_goal") or team_stats.get("Total Shots") or team_stats.get("Shots on Goal") or {}
        shots_total = _safe_int(shots.get("total") if isinstance(shots, dict) else shots)

        penalties = _safe_int(
            (team_stats.get("penalty_minutes") or {}).get("total")
            if isinstance(team_stats.get("penalty_minutes"), dict)
            else team_stats.get("penalty_minutes", 0)
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
            else team_stats.get("hits", 0)
        )

        blocked = _safe_int(
            (team_stats.get("blocked_shots") or {}).get("total")
            if isinstance(team_stats.get("blocked_shots"), dict)
            else team_stats.get("blocked_shots", 0)
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
# Games today (universal)
# ---------------------------------------------------------------------------

async def get_games_today(
    league_id: int = 50,
    sport_slug: str = "ice-hockey",
) -> List[Dict[str, Any]]:
    """Fetch all games for today for any sport."""
    slug = resolve_sport_slug(sport_slug) or sport_slug
    cfg = get_sport_config(slug)
    if not cfg:
        return []

    endpoints = get_endpoints(slug)
    fixtures_endpoint = endpoints.get("fixtures", "/games")

    # Get season for this league
    league_info = cfg.get("leagues", {}).get(league_id, {})
    season = league_info.get("season", 2025)

    try:
        today = date.today().isoformat()
        return await _api_get(slug, fixtures_endpoint, {
            "league": league_id,
            "season": season,
            "date": today,
        })
    except Exception:
        logger.exception("get_games_today failed for sport=%s league=%d", slug, league_id)
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
