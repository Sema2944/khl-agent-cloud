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
from datetime import date, datetime
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
# Team name normalization: Russian → English for api-sports.io
# ---------------------------------------------------------------------------

# KHL teams: Russian name → list of English search variants for api-sports.io
# Multiple variants tried in order; first successful match wins.
# Source: eliteprospects.com/league/khl/2024-2025 + hockeydb.com
_TEAM_NAME_MAP: Dict[str, List[str]] = {
    # ===== KHL (22–23 teams) =====
    "динамо москва": ["Dynamo Moscow", "Dynamo Moskva", "Dynamo"],
    "динамо мск": ["Dynamo Moscow", "Dynamo Moskva"],
    "динамо м": ["Dynamo Moscow", "Dynamo Moskva"],
    "динамо минск": ["Dinamo Minsk"],
    "динамо мн": ["Dinamo Minsk"],
    "динамо рига": ["Dinamo Riga"],
    "хк сочи": ["HC Sochi", "Sochi"],
    "сочи": ["HC Sochi", "Sochi"],
    "ак барс": ["Ak Bars Kazan", "Ak Bars", "Ak-Bars"],
    "ак барс казань": ["Ak Bars Kazan", "Ak Bars"],
    "лада": ["Lada Togliatti", "Lada"],
    "лада тольятти": ["Lada Togliatti", "Lada"],
    "ска": ["SKA St. Petersburg", "SKA Saint Petersburg", "SKA"],
    "ска спб": ["SKA St. Petersburg", "SKA"],
    "ска санкт-петербург": ["SKA St. Petersburg", "SKA"],
    "цска": ["CSKA Moscow", "CSKA Moskva", "CSKA"],
    "цска москва": ["CSKA Moscow", "CSKA Moskva", "CSKA"],
    "спартак": ["Spartak Moscow", "Spartak Moskva", "Spartak"],
    "спартак москва": ["Spartak Moscow", "Spartak Moskva"],
    "локомотив": ["Lokomotiv Yaroslavl", "Lokomotiv"],
    "локомотив ярославль": ["Lokomotiv Yaroslavl", "Lokomotiv"],
    "металлург": ["Metallurg Magnitogorsk", "Metallurg Mg", "Metallurg"],
    "металлург мг": ["Metallurg Magnitogorsk", "Metallurg"],
    "металлург магнитогорск": ["Metallurg Magnitogorsk"],
    "авангард": ["Avangard Omsk", "Avangard"],
    "авангард омск": ["Avangard Omsk", "Avangard"],
    "салават юлаев": ["Salavat Yulaev Ufa", "Salavat Yulaev"],
    "салават юлаев уфа": ["Salavat Yulaev Ufa", "Salavat Yulaev"],
    "трактор": ["Traktor Chelyabinsk", "Traktor"],
    "трактор челябинск": ["Traktor Chelyabinsk", "Traktor"],
    "сибирь": ["Sibir Novosibirsk", "Sibir"],
    "сибирь новосибирск": ["Sibir Novosibirsk", "Sibir"],
    "торпедо": ["Torpedo Nizhny Novgorod", "Torpedo"],
    "торпедо нижний новгород": ["Torpedo Nizhny Novgorod", "Torpedo"],
    "торпедо нн": ["Torpedo Nizhny Novgorod", "Torpedo"],
    "нефтехимик": ["Neftekhimik Nizhnekamsk", "Neftekhimik"],
    "нефтехимик нижнекамск": ["Neftekhimik Nizhnekamsk", "Neftekhimik"],
    "северсталь": ["Severstal Cherepovets", "Severstal"],
    "северсталь череповец": ["Severstal Cherepovets", "Severstal"],
    "витязь": ["Vityaz", "Vityaz Podolsk"],
    "витязь подольск": ["Vityaz", "Vityaz Podolsk"],
    "адмирал": ["Admiral Vladivostok", "Admiral"],
    "адмирал владивосток": ["Admiral Vladivostok", "Admiral"],
    "амур": ["Amur Khabarovsk", "Amur"],
    "амур хабаровск": ["Amur Khabarovsk", "Amur"],
    "барыс": ["Barys Astana", "Barys", "Barys Nur-Sultan"],
    "барыс астана": ["Barys Astana", "Barys"],
    "куньлунь": ["Kunlun Red Star", "Kunlun", "Shanghai Dragons"],
    "куньлунь ред стар": ["Kunlun Red Star", "Kunlun"],
    "автомобилист": ["Avtomobilist Yekaterinburg", "Avtomobilist"],
    "автомобилист екатеринбург": ["Avtomobilist Yekaterinburg", "Avtomobilist"],
    # ===== RPL (Russia football) =====
    "зенит": ["Zenit St. Petersburg", "Zenit"],
    "зенит спб": ["Zenit St. Petersburg", "Zenit"],
    "краснодар": ["FK Krasnodar", "Krasnodar"],
    "фк краснодар": ["FK Krasnodar", "Krasnodar"],
    "рубин": ["Rubin Kazan", "Rubin"],
    "рубин казань": ["Rubin Kazan", "Rubin"],
    "ростов": ["FK Rostov", "Rostov"],
    "фк ростов": ["FK Rostov", "Rostov"],
    "крылья советов": ["Krylya Sovetov", "Krylia Sovetov"],
    "факел": ["Fakel Voronezh", "Fakel"],
    "факел воронеж": ["Fakel Voronezh", "Fakel"],
    "оренбург": ["Orenburg", "FK Orenburg"],
    "фк оренбург": ["Orenburg", "FK Orenburg"],
    "ахмат": ["Akhmat Grozny", "Akhmat"],
    "ахмат грозный": ["Akhmat Grozny", "Akhmat"],
    "урал": ["Ural", "FK Ural"],
    "урал екатеринбург": ["Ural", "FK Ural"],
    "пари нн": ["Pari NN", "Pari Nizhny Novgorod"],
    "химки": ["Khimki", "FK Khimki"],
    "балтика": ["Baltika", "Baltika Kaliningrad"],
    "локомотив москва": ["Lokomotiv Moscow"],
    "цска москва футбол": ["CSKA Moscow"],
    "динамо москва футбол": ["Dynamo Moscow"],
    "спартак москва футбол": ["Spartak Moscow"],
}

# Prefixes to strip before lookup
_STRIP_PREFIXES = ("хк ", "фк ", "бк ", "hc ", "fc ", "bc ")


def _get_search_variants(name: str) -> List[str]:
    """
    Get list of English search variants for a team name.
    Checks mapping, strips prefixes, returns variants to try in order.
    """
    clean = name.strip()
    lower = clean.lower()

    # Direct mapping hit → return all variants
    if lower in _TEAM_NAME_MAP:
        return _TEAM_NAME_MAP[lower]

    # Strip prefix and try again
    for prefix in _STRIP_PREFIXES:
        if lower.startswith(prefix):
            stripped = lower[len(prefix):].strip()
            if stripped in _TEAM_NAME_MAP:
                return _TEAM_NAME_MAP[stripped]

    # No mapping → return original as single variant
    return [clean]


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
    Tries multiple English name variants from mapping.
    """
    slug = resolve_sport_slug(sport_slug) or sport_slug
    cache_key = f"{slug}:{team_name.lower().strip()}"

    if cache_key in _team_id_cache:
        cached = _team_id_cache[cache_key]
        logger.debug("resolve_team_id CACHE HIT: '%s' → %s (%s)", team_name, cached, slug)
        return cached

    # Get search variants from mapping
    variants = _get_search_variants(team_name)
    logger.info("resolve_team_id: '%s' → variants=%s (%s)", team_name, variants, slug)

    tid = None
    for variant in variants:
        tid = await search_team_id(variant, sport_slug)
        if tid is not None:
            logger.info("resolve_team_id: '%s' found via variant '%s' → id=%d", team_name, variant, tid)
            break

    # Last resort: try original name (if not in variants)
    if tid is None and team_name not in variants:
        logger.info("resolve_team_id: all variants failed, trying original '%s'", team_name)
        tid = await search_team_id(team_name, sport_slug)

    _team_id_cache[cache_key] = tid
    if tid is None:
        logger.warning("resolve_team_id: FAILED for '%s' (%s), tried: %s", team_name, slug, variants)
    return tid


async def search_team_id(
    team_name: str,
    sport_slug: str = "ice-hockey",
) -> Optional[int]:
    """Search for team ID by name on api-sports.io. Single attempt."""
    slug = resolve_sport_slug(sport_slug) or sport_slug

    try:
        results = await _api_get(slug, "/teams", {"search": team_name})
        if results:
            tid = results[0].get("id")
            tname = results[0].get("name", "?")
            logger.info("search_team_id: '%s' → id=%s name='%s' (%s)", team_name, tid, tname, slug)
            return tid

        # Try shorter name (first significant word, min 3 chars)
        words = team_name.split()
        if len(words) > 1:
            short = words[0]
            if len(short) >= 3:
                results = await _api_get(slug, "/teams", {"search": short})
                if results:
                    tid = results[0].get("id")
                    tname = results[0].get("name", "?")
                    logger.info("search_team_id: '%s' (short='%s') → id=%s name='%s' (%s)",
                                team_name, short, tid, tname, slug)
                    return tid

        logger.debug("search_team_id: NOT FOUND '%s' (%s)", team_name, slug)
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


# ===========================================================================
# PRIMARY API (api-sport.ru) — for hockey form/H2H
# Uses SPORT_API_BASE + SPORT_API_KEY (same as sport_api.py)
# ===========================================================================

_PRIMARY_BASE = os.getenv("SPORT_API_BASE", "").rstrip("/")
_PRIMARY_KEY = os.getenv("SPORT_API_KEY", "")
_PRIMARY_HEADER = os.getenv("SPORT_API_KEY_HEADER", "Authorization")
_PRIMARY_PREFIX = os.getenv("SPORT_API_KEY_PREFIX", "")


def _primary_headers() -> Dict[str, str]:
    if not _PRIMARY_KEY:
        return {}
    val = f"{_PRIMARY_PREFIX}{_PRIMARY_KEY}".strip()
    return {_PRIMARY_HEADER: val} if val else {}


async def _primary_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """GET request to primary API (api-sport.ru)."""
    if not _PRIMARY_BASE or not _PRIMARY_KEY:
        logger.debug("primary API not configured (SPORT_API_BASE/SPORT_API_KEY missing)")
        return None

    url = f"{_PRIMARY_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(url, headers=_primary_headers(), params=params or {})

            logger.info("primary_get: GET %s → HTTP %d", path, resp.status_code)

            if resp.status_code >= 400:
                logger.warning("primary_get: %s returned HTTP %d: %s",
                               path, resp.status_code, (resp.text or "")[:300])
                return None

            return resp.json()
    except Exception:
        logger.exception("primary_get failed: %s", path)
        return None


def _primary_unwrap_list(data: Any) -> List[Dict[str, Any]]:
    """Unwrap list from api-sport.ru response (handles data/response/results wrappers)."""
    if isinstance(data, list):
        return [i for i in data if isinstance(i, dict)]
    if isinstance(data, dict):
        for key in ("data", "response", "results", "items", "matches", "events", "list"):
            v = data.get(key)
            if isinstance(v, list):
                return [i for i in v if isinstance(i, dict)]
    return []


# ---------------------------------------------------------------------------
# Helpers for primary API match parsing
# ---------------------------------------------------------------------------

import re as _re
from datetime import timedelta

# In-memory cache for recent matches from api-sport.ru (shared across form/H2H)
_primary_matches_cache: Dict[str, Any] = {}


def _get_match_team_ids(m: Dict[str, Any]) -> tuple:
    """Extract (home_team_id, away_team_id) from a raw match dict."""
    home_team = m.get("homeTeam") or m.get("home_team") or m.get("home") or {}
    away_team = m.get("awayTeam") or m.get("away_team") or m.get("away") or {}
    h_id = home_team.get("id") if isinstance(home_team, dict) else None
    a_id = away_team.get("id") if isinstance(away_team, dict) else None
    try:
        h_id = int(h_id) if h_id is not None else None
    except (ValueError, TypeError):
        h_id = None
    try:
        a_id = int(a_id) if a_id is not None else None
    except (ValueError, TypeError):
        a_id = None
    return h_id, a_id


def _is_finished_match(m: Dict[str, Any]) -> bool:
    """Check if a match has a final score (finished)."""
    status = str(m.get("status") or m.get("state") or m.get("matchStatus") or "").lower()
    # Definitely finished
    if any(s in status for s in ("finish", "ended", "ft", "completed", "closed", "after", "aet", "pen")):
        return True
    # Definitely not finished
    if any(s in status for s in ("ns", "notstarted", "not_started", "live", "inprogress",
                                  "1p", "2p", "3p", "ot", "scheduled", "postponed", "cancelled")):
        return False
    # Fallback: check if score exists
    score = m.get("score") or m.get("scores") or m.get("result")
    if score:
        if isinstance(score, str) and _re.search(r"\d+\s*[:\-]\s*\d+", score):
            return True
        if isinstance(score, dict) and score.get("home") is not None:
            return True
    h_s = m.get("homeScore") or m.get("home_score")
    a_s = m.get("awayScore") or m.get("away_score")
    if h_s is not None and a_s is not None:
        return True
    return False


def _get_match_date_str(m: Dict[str, Any]) -> str:
    """Extract date string from match for sorting (newest first)."""
    return str(
        m.get("dateEvent") or m.get("startTime") or m.get("start_date")
        or m.get("startDate") or m.get("date") or m.get("time") or ""
    )


def _extract_score_pair(m: Dict[str, Any]) -> tuple:
    """Extract (home_score, away_score) ints from a match dict.
    Handles many api-sport.ru response formats."""

    # --- 1. Direct numeric fields ---
    for hk, ak in (
        ("homeScore", "awayScore"),
        ("home_score", "away_score"),
        ("scoreHome", "scoreAway"),
        ("goalsHome", "goalsAway"),
    ):
        h_val = m.get(hk)
        a_val = m.get(ak)
        if h_val is not None and a_val is not None:
            return (_safe_int(h_val), _safe_int(a_val))

    # --- 2. "score" field ---
    score = m.get("score")
    if isinstance(score, str) and score.strip():
        sm = _re.match(r"(\d+)\s*[:\-]\s*(\d+)", score.strip())
        if sm:
            return (int(sm.group(1)), int(sm.group(2)))
    if isinstance(score, dict):
        # score.home / score.away (can be int or nested dict)
        h = score.get("home")
        a = score.get("away")
        if h is not None and a is not None:
            h = h if not isinstance(h, dict) else h.get("total", h.get("current", 0))
            a = a if not isinstance(a, dict) else a.get("total", a.get("current", 0))
            return (_safe_int(h), _safe_int(a))
        # score.fullTime.home / fullTime.away
        ft = score.get("fullTime") or score.get("full_time") or score.get("ft")
        if isinstance(ft, dict):
            return (_safe_int(ft.get("home", 0)), _safe_int(ft.get("away", 0)))
        # score.current.home etc.
        cur = score.get("current")
        if isinstance(cur, dict):
            return (_safe_int(cur.get("home", 0)), _safe_int(cur.get("away", 0)))

    # --- 3. "scores" field ---
    scores = m.get("scores")
    if isinstance(scores, dict):
        h = scores.get("home")
        a = scores.get("away")
        if h is not None and a is not None:
            h = h if not isinstance(h, dict) else h.get("total", 0)
            a = a if not isinstance(a, dict) else a.get("total", 0)
            return (_safe_int(h), _safe_int(a))

    # --- 4. "result" field ---
    result = m.get("result")
    if isinstance(result, str) and result.strip():
        sm = _re.match(r"(\d+)\s*[:\-]\s*(\d+)", result.strip())
        if sm:
            return (int(sm.group(1)), int(sm.group(2)))
    if isinstance(result, dict):
        h = result.get("home") or result.get("homeScore")
        a = result.get("away") or result.get("awayScore")
        if h is not None and a is not None:
            return (_safe_int(h), _safe_int(a))

    # --- 5. Nested in homeTeam/awayTeam objects ---
    ht = m.get("homeTeam") or m.get("home_team") or m.get("home") or {}
    at = m.get("awayTeam") or m.get("away_team") or m.get("away") or {}
    if isinstance(ht, dict) and isinstance(at, dict):
        h = ht.get("score") or ht.get("goals") or ht.get("points")
        a = at.get("score") or at.get("goals") or at.get("points")
        if h is not None and a is not None:
            return (_safe_int(h), _safe_int(a))

    # --- 6. "winner" field as fallback (no exact score) ---
    winner = m.get("winner") or m.get("winnerCode")
    if winner is not None:
        w = str(winner).lower().strip()
        if w in ("home", "1", "homeTeam"):
            return (1, 0)
        if w in ("away", "2", "awayTeam"):
            return (0, 1)
        if w in ("draw", "x", "0"):
            return (0, 0)

    return (0, 0)


# ---------------------------------------------------------------------------
# Fetch recent matches from api-sport.ru (cached, shared)
# ---------------------------------------------------------------------------

async def _fetch_recent_matches_primary(
    sport_slug: str = "ice-hockey",
    days_back: int = 60,
) -> List[Dict[str, Any]]:
    """
    Fetch all finished matches from api-sport.ru for the last N days.
    Strategy: date range params → day-by-day fallback. Cached for 6 hours.
    """
    cache_key = f"primary_recent:{sport_slug}:{days_back}"
    now = datetime.utcnow()
    if cache_key in _primary_matches_cache:
        cached_data, expires = _primary_matches_cache[cache_key]
        if now < expires:
            logger.debug("_fetch_recent_primary: CACHE HIT (%d matches)", len(cached_data))
            return cached_data

    today = date.today()
    date_from_s = (today - timedelta(days=days_back)).isoformat()
    date_to_s = today.isoformat()

    sports = ["ice-hockey", "hockey"] if sport_slug in ("ice-hockey", "hockey") else [sport_slug]

    # === Strategy 1: Date range in one request ===
    for sport in sports:
        for path in (f"/v2/{sport}/matches", f"/v2/{sport}/events"):
            for params in (
                {"dateFrom": date_from_s, "dateTo": date_to_s},
                {"from": date_from_s, "to": date_to_s},
                {"startDate": date_from_s, "endDate": date_to_s},
                {"date_from": date_from_s, "date_to": date_to_s},
            ):
                raw = await _primary_get(path, params)
                if raw is None:
                    continue
                items = _primary_unwrap_list(raw)
                # Date range should return more than a single day's matches
                if len(items) > 10:
                    finished = [m for m in items if _is_finished_match(m)]
                    finished.sort(key=_get_match_date_str, reverse=True)
                    _primary_matches_cache[cache_key] = (finished, now + timedelta(hours=6))
                    logger.info("_fetch_recent_primary: %s range → %d items, %d finished",
                                path, len(items), len(finished))
                    return finished

    # === Strategy 2: Day-by-day (max 14 days to limit API calls) ===
    max_days = min(days_back, 14)
    logger.info("_fetch_recent_primary: date range failed, trying day-by-day (%d days)", max_days)

    all_finished: List[Dict[str, Any]] = []
    working_path: Optional[str] = None

    for i in range(max_days):
        day_s = (today - timedelta(days=i)).isoformat()

        if working_path is None:
            # First iteration: probe to find working endpoint
            for sport in sports:
                for path in (f"/v2/{sport}/matches", f"/v2/{sport}/events"):
                    raw = await _primary_get(path, {"date": day_s})
                    if raw is not None:
                        items = _primary_unwrap_list(raw)
                        if items:
                            working_path = path
                            all_finished.extend(m for m in items if _is_finished_match(m))
                            logger.info("_fetch_recent_primary: working endpoint=%s", path)
                            break
                if working_path:
                    break
            if not working_path:
                logger.warning("_fetch_recent_primary: no working endpoint found on %s", day_s)
                return []
        else:
            raw = await _primary_get(working_path, {"date": day_s})
            if raw is not None:
                items = _primary_unwrap_list(raw)
                all_finished.extend(m for m in items if _is_finished_match(m))

    all_finished.sort(key=_get_match_date_str, reverse=True)
    if all_finished:
        _primary_matches_cache[cache_key] = (all_finished, now + timedelta(hours=6))
    logger.info("_fetch_recent_primary: day-by-day → %d finished matches", len(all_finished))
    return all_finished


# ---------------------------------------------------------------------------
# Hockey form via api-sport.ru
# ---------------------------------------------------------------------------

async def get_team_form_primary(
    team_id: int,
    sport_slug: str = "ice-hockey",
    last: int = 10,
) -> Optional[TeamForm]:
    """
    Fetch team form from api-sport.ru.
    api-sport.ru has no dedicated team-matches endpoint, so we:
    1. Try query params (?team_id=X, ?team=X, etc.) on /matches
    2. Fall back to fetching all recent matches and filtering by team_id
    """
    sports = ["ice-hockey", "hockey"] if sport_slug in ("ice-hockey", "hockey") else [sport_slug]

    # === Strategy 1: Direct team filter params ===
    for sport in sports:
        path = f"/v2/{sport}/matches"
        for param_name in ("team_id", "team", "teams", "teamId"):
            data = await _primary_get(path, {param_name: team_id})
            if data is None:
                continue
            items = _primary_unwrap_list(data)
            if not items:
                continue
            # API may ignore ?last=, so sort + limit client-side
            finished = [m for m in items if _is_finished_match(m)]
            finished.sort(key=_get_match_date_str, reverse=True)
            recent = finished[:last]
            if recent:
                logger.info("get_team_form_primary: %s?%s=%d → %d total, %d finished, using %d recent",
                            path, param_name, team_id, len(items), len(finished), len(recent))
                return _parse_primary_form(recent, team_id)

    # === Strategy 2: Date range + client-side filter ===
    all_matches = await _fetch_recent_matches_primary(sport_slug)
    if all_matches:
        team_matches = [m for m in all_matches if team_id in _get_match_team_ids(m)]
        if team_matches:
            team_matches.sort(key=_get_match_date_str, reverse=True)
            logger.info("get_team_form_primary: filtered %d/%d matches for team=%d",
                        len(team_matches), len(all_matches), team_id)
            return _parse_primary_form(team_matches[:last], team_id)

    logger.warning("get_team_form_primary: no data for team=%d sport=%s", team_id, sport_slug)
    return None


def _parse_primary_form(matches: List[Dict[str, Any]], team_id: int) -> TeamForm:
    """Parse list of recent matches from api-sport.ru into TeamForm."""
    import json as _json

    # === RAW LOG: dump first 3 matches to see actual JSON structure ===
    for idx, sample in enumerate(matches[:3]):
        try:
            # Log top-level keys + full JSON (truncated to 1500 chars)
            raw_str = _json.dumps(sample, ensure_ascii=False, default=str)
            logger.info("RAW_MATCH[%d] team=%d keys=%s JSON=%.1500s",
                        idx, team_id, list(sample.keys()), raw_str)
        except Exception:
            logger.info("RAW_MATCH[%d] team=%d keys=%s (json dump failed)", idx, team_id, list(sample.keys())[:20])

    form = TeamForm()
    wins = losses = otl = 0
    home_w = home_l = away_w = away_l = 0
    goals_for = goals_against = 0
    streak_chars = []

    for idx, m in enumerate(matches):
        h_id, a_id = _get_match_team_ids(m)
        h_score, a_score = _extract_score_pair(m)

        # Log score extraction for first 5 matches
        if idx < 5:
            logger.info("FORM_PARSE[%d] team=%d h_id=%s a_id=%s h_score=%d a_score=%d",
                        idx, team_id, h_id, a_id, h_score, a_score)

        # Determine if this team is home or away
        is_home = (h_id == team_id) if h_id is not None else (a_id != team_id)

        my_goals = h_score if is_home else a_score
        opp_goals = a_score if is_home else h_score
        goals_for += my_goals
        goals_against += opp_goals

        if my_goals > opp_goals:
            wins += 1
            if is_home:
                home_w += 1
            else:
                away_w += 1
            streak_chars.append("W")
        elif my_goals < opp_goals:
            losses += 1
            if is_home:
                home_l += 1
            else:
                away_l += 1
            streak_chars.append("L")
        else:
            otl += 1
            streak_chars.append("D")

    total = wins + losses + otl or 1
    form.wins = wins
    form.losses = losses
    form.otl = otl
    form.goals_per_game = round(goals_for / total, 1)
    form.goals_against_per_game = round(goals_against / total, 1)
    form.last_10 = f"{wins}W-{losses}L" + (f"-{otl}OTL" if otl else "")
    form.home_record = f"{home_w}W-{home_l}L"
    form.away_record = f"{away_w}W-{away_l}L"

    # Streak
    if streak_chars:
        last_char = streak_chars[0]  # most recent match first
        count = 0
        for c in streak_chars:
            if c == last_char:
                count += 1
            else:
                break
        form.streak = f"{count}{last_char}"

    logger.info("_parse_primary_form: team=%d → %s streak=%s gpg=%.1f gapg=%.1f (%d matches)",
                team_id, form.last_10, form.streak, form.goals_per_game,
                form.goals_against_per_game, len(matches))
    return form


# ---------------------------------------------------------------------------
# Hockey H2H via api-sport.ru
# ---------------------------------------------------------------------------

async def get_h2h_primary(
    team1_id: int,
    team2_id: int,
    sport_slug: str = "ice-hockey",
    last: int = 10,
) -> Optional[H2HData]:
    """
    Fetch H2H from api-sport.ru.
    1. Try direct H2H query params (?h2h=X-Y)
    2. Fall back to fetching all recent matches and filtering by both team IDs
    """
    sports = ["ice-hockey", "hockey"] if sport_slug in ("ice-hockey", "hockey") else [sport_slug]

    # === Strategy 1: Direct H2H param ===
    for sport in sports:
        path = f"/v2/{sport}/matches"
        for params in (
            {"h2h": f"{team1_id}-{team2_id}"},
            {"h2h": f"{team2_id}-{team1_id}"},
        ):
            data = await _primary_get(path, params)
            if data is None:
                continue
            items = _primary_unwrap_list(data)
            logger.info("get_h2h_primary: %s?%s → %d raw items", path, params, len(items))
            # Verify these are actually H2H matches (API might return unrelated data)
            h2h_items = []
            for m in items:
                ids = _get_match_team_ids(m)
                if team1_id in ids and team2_id in ids:
                    if _is_finished_match(m):
                        h2h_items.append(m)
            if h2h_items:
                h2h_items.sort(key=_get_match_date_str, reverse=True)
                logger.info("get_h2h_primary: %s?h2h → %d verified H2H matches", path, len(h2h_items))
                return _parse_primary_h2h(h2h_items[:last], team1_id, team2_id)
            elif items:
                # Log why H2H filter failed — items exist but no H2H match
                import json as _json
                for si, sample in enumerate(items[:2]):
                    try:
                        raw_s = _json.dumps(sample, ensure_ascii=False, default=str)
                        logger.info("H2H_RAW[%d] keys=%s JSON=%.1000s", si, list(sample.keys()), raw_s)
                    except Exception:
                        logger.info("H2H_RAW[%d] keys=%s", si, list(sample.keys())[:20])

    # === Strategy 2: Date range + client-side filter ===
    all_matches = await _fetch_recent_matches_primary(sport_slug)
    if all_matches:
        h2h_matches = []
        for m in all_matches:
            ids = _get_match_team_ids(m)
            if team1_id in ids and team2_id in ids:
                h2h_matches.append(m)
        if h2h_matches:
            h2h_matches.sort(key=_get_match_date_str, reverse=True)
            logger.info("get_h2h_primary: filtered %d H2H matches for %d vs %d",
                        len(h2h_matches), team1_id, team2_id)
            return _parse_primary_h2h(h2h_matches[:last], team1_id, team2_id)

    logger.warning("get_h2h_primary: no H2H for %d vs %d sport=%s", team1_id, team2_id, sport_slug)
    return None


def _parse_primary_h2h(
    matches: List[Dict[str, Any]], home_id: int, away_id: int
) -> H2HData:
    """Parse H2H matches from api-sport.ru into H2HData."""
    import json as _json

    # RAW LOG first 2 H2H matches
    for idx, sample in enumerate(matches[:2]):
        try:
            raw_s = _json.dumps(sample, ensure_ascii=False, default=str)
            logger.info("H2H_PARSE_RAW[%d] %d vs %d JSON=%.1500s", idx, home_id, away_id, raw_s)
        except Exception:
            pass

    h2h = H2HData(total_games=len(matches))
    total_goals = 0

    for idx, m in enumerate(matches):
        h_id, _ = _get_match_team_ids(m)
        h_score, a_score = _extract_score_pair(m)
        if idx < 3:
            logger.info("H2H_PARSE[%d] h_id=%s h_score=%d a_score=%d", idx, h_id, h_score, a_score)

        total_goals += h_score + a_score

        if h_score > a_score:
            if h_id == home_id:
                h2h.home_wins += 1
            else:
                h2h.away_wins += 1
        elif a_score > h_score:
            if h_id == home_id:
                h2h.away_wins += 1
            else:
                h2h.home_wins += 1
        else:
            h2h.draws += 1

    if h2h.total_games > 0:
        h2h.avg_total = round(total_goals / h2h.total_games, 1)

    # Last result
    if matches:
        last_m = matches[0]
        ht = last_m.get("homeTeam") or last_m.get("home_team") or last_m.get("home") or {}
        h_name = (ht.get("name") or "?") if isinstance(ht, dict) else "?"
        h_s, a_s = _extract_score_pair(last_m)
        h2h.last_result = f"{h_name} {h_s}:{a_s}"

    logger.info("_parse_primary_h2h: %d vs %d → total=%d hw=%d aw=%d draws=%d avg=%.1f",
                home_id, away_id, h2h.total_games, h2h.home_wins,
                h2h.away_wins, h2h.draws, h2h.avg_total)
    return h2h
