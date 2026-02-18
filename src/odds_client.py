# src/odds_client.py
"""
The Odds API client.
https://the-odds-api.com

Env: THE_ODDS_API_KEY
Free tier: 500 requests/month.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from .data_collector import OddsData

logger = logging.getLogger(__name__)

API_KEY = os.getenv("THE_ODDS_API_KEY", "")
BASE_URL = "https://api.the-odds-api.com/v4"
TIMEOUT = 10.0

# Mapping: our sport_slug → Odds API sport key
SPORT_MAP: Dict[str, List[str]] = {
    "ice-hockey": [
        "icehockey_russia_khl",
        "icehockey_nhl",
        "icehockey_sweden_shl",
        "icehockey_finland_liiga",
    ],
    "football": [
        "soccer_russia_premier_league",
        "soccer_epl",
        "soccer_spain_la_liga",
        "soccer_germany_bundesliga",
        "soccer_italy_serie_a",
        "soccer_france_ligue_one",
        "soccer_uefa_champs_league",
        "soccer_uefa_europa_league",
    ],
    "basketball": [
        "basketball_euroleague",
        "basketball_nba",
    ],
}


async def _api_get(path: str, params: Dict[str, Any]) -> Any:
    """Make authenticated GET request to The Odds API."""
    if not API_KEY:
        logger.debug("THE_ODDS_API_KEY not set, skipping odds fetch")
        return None

    params["apiKey"] = API_KEY
    url = f"{BASE_URL}{path}"

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()

        # Log remaining requests (returned in headers)
        remaining = resp.headers.get("x-requests-remaining")
        if remaining is not None:
            logger.info("Odds API requests remaining: %s", remaining)

        return resp.json()


async def get_odds_for_sport(sport_key: str) -> List[Dict[str, Any]]:
    """Fetch all upcoming events with odds for a sport key."""
    data = await _api_get(
        f"/sports/{sport_key}/odds",
        {
            "regions": "eu",
            "markets": "h2h,totals",
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        },
    )
    return data if isinstance(data, list) else []


async def get_match_odds(
    home_team: str,
    away_team: str,
    sport_slug: str = "ice-hockey",
) -> Optional[OddsData]:
    """
    Find odds for a specific match by team names.
    Tries all sport keys mapped to the slug.
    Returns OddsData or None.
    """
    if not API_KEY:
        return None

    sport_keys = SPORT_MAP.get(sport_slug, [])
    if not sport_keys:
        # Try the slug directly as API key
        sport_keys = [sport_slug]

    home_lower = home_team.lower().strip()
    away_lower = away_team.lower().strip()

    for sport_key in sport_keys:
        try:
            events = await get_odds_for_sport(sport_key)
            if not events:
                continue

            for event in events:
                ev_home = (event.get("home_team") or "").lower()
                ev_away = (event.get("away_team") or "").lower()

                # Fuzzy match: check if team name is contained
                home_match = (
                    home_lower in ev_home or ev_home in home_lower
                    or _fuzzy_team(home_lower, ev_home)
                )
                away_match = (
                    away_lower in ev_away or ev_away in away_lower
                    or _fuzzy_team(away_lower, ev_away)
                )

                if home_match and away_match:
                    return _parse_event_odds(event)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                logger.error("Odds API: invalid API key")
                return None
            if e.response.status_code == 429:
                logger.warning("Odds API: rate limit hit")
                return None
            logger.warning("Odds API error for %s: %s", sport_key, e)
        except Exception:
            logger.exception("Odds API failed for %s", sport_key)

    return None


async def get_all_odds_today(sport_slug: str = "ice-hockey") -> List[Dict[str, Any]]:
    """Fetch all events with odds for a sport slug (all mapped keys)."""
    if not API_KEY:
        return []

    sport_keys = SPORT_MAP.get(sport_slug, [])
    all_events = []

    for sport_key in sport_keys:
        try:
            events = await get_odds_for_sport(sport_key)
            for ev in events:
                ev["_sport_key"] = sport_key
            all_events.extend(events)
        except Exception:
            logger.exception("Odds API: all_odds failed for %s", sport_key)

    return all_events


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_event_odds(event: Dict[str, Any]) -> OddsData:
    """Parse The Odds API event into our OddsData dataclass."""
    odds = OddsData()
    bookmakers = event.get("bookmakers") or []

    if not bookmakers:
        return odds

    # Prefer Pinnacle > bet365 > first available
    bk = _pick_bookmaker(bookmakers)
    odds.bookmaker = bk.get("title", "")

    for market in bk.get("markets") or []:
        key = market.get("key", "")

        if key == "h2h":
            for outcome in market.get("outcomes") or []:
                name = (outcome.get("name") or "").lower()
                price = outcome.get("price", 0.0)
                if name == (event.get("home_team") or "").lower():
                    odds.home_win = price
                elif name == (event.get("away_team") or "").lower():
                    odds.away_win = price
                elif name == "draw":
                    odds.draw = price

        elif key == "totals":
            for outcome in market.get("outcomes") or []:
                name = (outcome.get("name") or "").lower()
                odds.total_line = outcome.get("point", 0.0)
                if name == "over":
                    odds.total_over = outcome.get("price", 0.0)
                elif name == "under":
                    odds.total_under = outcome.get("price", 0.0)

    return odds


def _pick_bookmaker(bookmakers: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pick best bookmaker from list (Pinnacle preferred)."""
    preferred = ["pinnacle", "bet365", "1xbet", "marathonbet", "betfair"]
    by_key = {bk.get("key", "").lower(): bk for bk in bookmakers}

    for name in preferred:
        if name in by_key:
            return by_key[name]

    return bookmakers[0]


def _fuzzy_team(name_a: str, name_b: str) -> bool:
    """Simple fuzzy match: check if significant words overlap."""
    words_a = set(name_a.split())
    words_b = set(name_b.split())
    # Remove very common words
    stop = {"хк", "фк", "бк", "fc", "hc", "bc", "ск", "fk"}
    words_a -= stop
    words_b -= stop
    if not words_a or not words_b:
        return False
    overlap = words_a & words_b
    return len(overlap) >= 1
