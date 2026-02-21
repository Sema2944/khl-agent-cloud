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

# Mapping: our sport_slug → Odds API sport key (from centralized config)
def _build_sport_map() -> Dict[str, List[str]]:
    try:
        from .sports_config import get_enabled_sports
        return {k: v.get("odds_keys", []) for k, v in get_enabled_sports().items() if v.get("odds_keys")}
    except Exception:
        return {
            "ice-hockey": ["icehockey_nhl", "icehockey_sweden_hockey_league", "icehockey_liiga"],
            "football": ["soccer_epl", "soccer_russia_premier_league", "soccer_spain_la_liga",
                         "soccer_germany_bundesliga", "soccer_italy_serie_a", "soccer_uefa_champs_league"],
            "basketball": ["basketball_nba", "basketball_euroleague"],
            "tennis": ["tennis_atp_australian_open", "tennis_atp_french_open", "tennis_atp_us_open",
                       "tennis_wta_australian_open", "tennis_wta_french_open", "tennis_wta_us_open"],
            "mma": ["mma_mixed_martial_arts"],
        }

SPORT_MAP: Dict[str, List[str]] = _build_sport_map()

# ---------------------------------------------------------------------------
# Leagues NOT covered by The Odds API — skip to avoid wasting API requests.
# КХЛ, ВХЛ и др. лиги отсутствуют в The Odds API → не тратим запросы.
# ---------------------------------------------------------------------------
_ODDS_API_SKIP_LEAGUES: Dict[str, set] = {
    "ice-hockey": {"khl", "кхл", "vhl", "вхл", "del", "extraliga", "czech extraliga",
                    "mhl", "мхл", "ice hockey league"},
}


def should_skip_odds_api(sport_slug: str, league: str) -> bool:
    """Check if this league is NOT covered by The Odds API (avoid wasting requests)."""
    if not league:
        return False
    skip_set = _ODDS_API_SKIP_LEAGUES.get(sport_slug, set())
    if not skip_set:
        return False
    league_lower = league.lower().strip()
    return any(skip_name in league_lower for skip_name in skip_set)


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


# ---------------------------------------------------------------------------
# Fallback #1: Parse odds_base from primary API (api-sport.ru)
# ---------------------------------------------------------------------------

def parse_odds_base(odds_base: Dict[str, Any]) -> Optional[OddsData]:
    """
    Convert odds_base from primary API (api-sport.ru) into OddsData.
    Format: {"markets": [{"name": "1X2", "choices": [{"name": "1", "odd": "1.85"}, ...]}]}
    """
    if not isinstance(odds_base, dict):
        return None

    markets = odds_base.get("markets")
    if not isinstance(markets, list) or not markets:
        return None

    odds = OddsData(bookmaker="api-sport.ru")

    for market in markets:
        if not isinstance(market, dict):
            continue

        mname = (market.get("name") or "").lower()
        choices = market.get("choices") or market.get("outcomes") or []

        # --- 1X2 / Moneyline ---
        if any(k in mname for k in ("1x2", "1 x 2", "moneyline", "match winner", "result")):
            for ch in choices:
                if not isinstance(ch, dict):
                    continue
                cname = str(ch.get("name") or "").strip()
                codd = _safe_float(ch.get("odd") or ch.get("price") or ch.get("value"))
                if codd <= 0:
                    continue

                if cname in ("1", "W1", "Home", "П1"):
                    odds.home_win = codd
                elif cname in ("2", "W2", "Away", "П2"):
                    odds.away_win = codd
                elif cname.upper() in ("X", "DRAW", "Ничья"):
                    odds.draw = codd

        # --- Total Over/Under ---
        if any(k in mname for k in ("total", "over", "under", "тотал")):
            for ch in choices:
                if not isinstance(ch, dict):
                    continue
                cname = str(ch.get("name") or "").strip().lower()
                codd = _safe_float(ch.get("odd") or ch.get("price") or ch.get("value"))
                if codd <= 0:
                    continue

                # Extract total line from name like "Over 5.5" or "Тотал Б 5.5"
                import re
                line_match = re.search(r"(\d+\.?\d*)", cname)
                if line_match:
                    odds.total_line = float(line_match.group(1))

                if any(k in cname for k in ("over", "больше", "б ")):
                    odds.total_over = codd
                elif any(k in cname for k in ("under", "меньше", "м ")):
                    odds.total_under = codd

    if odds.home_win > 0 and odds.away_win > 0:
        logger.info("parse_odds_base: OK hw=%.2f draw=%s aw=%.2f total=%.1f",
                     odds.home_win, odds.draw, odds.away_win, odds.total_line)
        return odds

    logger.debug("parse_odds_base: incomplete data from %d markets", len(markets))
    return None


def _safe_float(val: Any) -> float:
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Fallback #2: api-sports.io hockey /odds endpoint
# ---------------------------------------------------------------------------

API_SPORTS_KEY = os.getenv("API_SPORTS_KEY", "")


async def get_odds_api_sports(
    match_id: str,
    sport_slug: str = "ice-hockey",
) -> Optional[OddsData]:
    """
    Try api-sports.io /odds or /bets endpoint for match odds.
    Works for KHL and other leagues not covered by The Odds API.
    """
    if not API_SPORTS_KEY:
        return None

    try:
        from .sports_config import get_api_base
    except ImportError:
        return None

    base = get_api_base(sport_slug)
    if not base:
        return None

    headers = {"x-apisports-key": API_SPORTS_KEY}

    # Try /odds first, then /bets
    for endpoint in ("/odds", "/bets"):
        try:
            url = f"{base}{endpoint}"
            params = {"game": match_id}

            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(url, headers=headers, params=params)

                logger.info("api-sports odds: GET %s?game=%s → HTTP %d",
                            endpoint, match_id, resp.status_code)

                if resp.status_code != 200:
                    continue

                data = resp.json()
                response = data.get("response")

                if not response:
                    logger.info("api-sports odds: %s empty response for game=%s", endpoint, match_id)
                    continue

                # Log raw structure for debugging
                if isinstance(response, list) and response:
                    first = response[0]
                    logger.info("api-sports odds: %s returned %d items, first_keys=%s",
                                endpoint, len(response),
                                list(first.keys())[:15] if isinstance(first, dict) else type(first).__name__)
                elif isinstance(response, dict):
                    logger.info("api-sports odds: %s returned dict, keys=%s",
                                endpoint, list(response.keys())[:15])

                odds = _parse_api_sports_odds(response)
                if odds:
                    return odds

        except Exception:
            logger.exception("api-sports odds: %s failed for game=%s", endpoint, match_id)

    return None


def _parse_api_sports_odds(response: Any) -> Optional[OddsData]:
    """Parse api-sports.io odds/bets response into OddsData."""
    items = response if isinstance(response, list) else [response]

    odds = OddsData(bookmaker="api-sports.io")

    for item in items:
        if not isinstance(item, dict):
            continue

        bookmakers = item.get("bookmakers") or []
        # Also handle direct odds structure
        if not bookmakers and item.get("values"):
            bookmakers = [item]

        for bk in bookmakers:
            if not isinstance(bk, dict):
                continue

            bets = bk.get("bets") or bk.get("markets") or []
            for bet in bets:
                if not isinstance(bet, dict):
                    continue

                bet_name = str(bet.get("name") or "").lower()
                values = bet.get("values") or bet.get("outcomes") or bet.get("odds") or []

                # Match Winner / 1X2
                if any(k in bet_name for k in ("match winner", "1x2", "home/away")):
                    for v in values:
                        if not isinstance(v, dict):
                            continue
                        vname = str(v.get("value") or v.get("name") or "").strip().lower()
                        vodd = _safe_float(v.get("odd") or v.get("price"))
                        if vodd <= 0:
                            continue
                        if vname in ("home", "1"):
                            odds.home_win = vodd
                        elif vname in ("away", "2"):
                            odds.away_win = vodd
                        elif vname in ("draw", "x"):
                            odds.draw = vodd

                # Over/Under / Total
                if any(k in bet_name for k in ("over/under", "total", "goals")):
                    for v in values:
                        if not isinstance(v, dict):
                            continue
                        vname = str(v.get("value") or v.get("name") or "").strip().lower()
                        vodd = _safe_float(v.get("odd") or v.get("price"))
                        if vodd <= 0:
                            continue
                        if "over" in vname:
                            odds.total_over = vodd
                            import re
                            m = re.search(r"(\d+\.?\d*)", vname)
                            if m:
                                odds.total_line = float(m.group(1))
                        elif "under" in vname:
                            odds.total_under = vodd

    if odds.home_win > 0 and odds.away_win > 0:
        logger.info("_parse_api_sports_odds: OK hw=%.2f draw=%s aw=%.2f total=%.1f",
                     odds.home_win, odds.draw, odds.away_win, odds.total_line)
        return odds

    return None
