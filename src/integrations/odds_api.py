# src/integrations/odds_api.py
"""
The Odds API client — fetch bookmaker odds for sports events.
https://the-odds-api.com/
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

ODDS_API_KEY = (os.getenv("ODDS_API_KEY") or "").strip()
ODDS_API_URL = "https://api.the-odds-api.com/v4"

def _get_api_key() -> str:
    """Get API key — re-read from env if module-level was empty."""
    global ODDS_API_KEY
    if not ODDS_API_KEY:
        ODDS_API_KEY = (os.getenv("ODDS_API_KEY") or "").strip()
    return ODDS_API_KEY

# Betly sport_slug → The Odds API sport key(s)
# Each sport may need multiple keys to cover different leagues.
SPORT_KEY_MAP: Dict[str, List[str]] = {
    "football": [
        "soccer_epl",
        "soccer_spain_la_liga",
        "soccer_germany_bundesliga",
        "soccer_italy_serie_a",
        "soccer_france_ligue_one",
        "soccer_uefa_champs_league",
        "soccer_uefa_europa_league",
    ],
    "ice-hockey": [
        "icehockey_nhl",
    ],
    "basketball": [
        "basketball_nba",
        "basketball_euroleague",
    ],
    "tennis": [
        "tennis_atp_french_open",
        "tennis_wta_french_open",
    ],
    "mma": [
        "mma_mixed_martial_arts",
    ],
}

BOOKMAKER_PRIORITY = ["pinnacle", "bet365", "onexbet", "marathonbet", "betfair"]


def _fuzzy_match_teams(event: Dict[str, Any], home: str, away: str) -> bool:
    """Check if event matches by team names (fuzzy)."""
    ev_home = (event.get("home_team") or "").lower().strip()
    ev_away = (event.get("away_team") or "").lower().strip()
    h = home.lower().strip()
    a = away.lower().strip()

    # Exact or substring match
    if (h in ev_home or ev_home in h) and (a in ev_away or ev_away in a):
        return True
    # Try reversed
    if (h in ev_away or ev_away in h) and (a in ev_home or ev_home in a):
        return True
    return False


async def fetch_odds_for_sport(
    sport_slug: str,
    *,
    bookmakers: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch all upcoming odds for a sport from The Odds API.

    Returns raw events list with bookmaker odds.
    """
    api_key = _get_api_key()
    if not api_key:
        logger.warning("OddsAPI: ODDS_API_KEY not set — skipping odds fetch for %s", sport_slug)
        return []

    sport_keys = SPORT_KEY_MAP.get(sport_slug, [])
    if not sport_keys:
        logger.debug("OddsAPI: no sport keys mapped for %s", sport_slug)
        return []

    bm_str = bookmakers or ",".join(BOOKMAKER_PRIORITY)
    all_events: List[Dict[str, Any]] = []

    logger.info("OddsAPI: fetching %s, key_present=True, sport_keys=%s", sport_slug, sport_keys)

    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        for sport_key in sport_keys:
            try:
                resp = await client.get(
                    f"{ODDS_API_URL}/sports/{sport_key}/odds/",
                    params={
                        "apiKey": api_key,
                        "regions": "eu",
                        "markets": "h2h,totals",
                        "bookmakers": bm_str,
                        "oddsFormat": "decimal",
                    },
                )
                remaining = resp.headers.get("x-requests-remaining", "?")
                used = resp.headers.get("x-requests-used", "?")
                logger.info(
                    "OddsAPI: %s HTTP %d, events=%s, used=%s, remaining=%s",
                    sport_key, resp.status_code,
                    len(resp.json()) if resp.status_code == 200 else "N/A",
                    used, remaining,
                )

                if resp.status_code != 200:
                    logger.warning(
                        "OddsAPI: %s HTTP %d: %s",
                        sport_key, resp.status_code, (resp.text or "")[:300],
                    )
                    continue

                events = resp.json()
                if isinstance(events, list):
                    all_events.extend(events)

            except Exception:
                logger.exception("OddsAPI: fetch failed for %s", sport_key)

    logger.info("OddsAPI: %s → %d events total", sport_slug, len(all_events))
    return all_events


def parse_event_odds(event: Dict[str, Any]) -> Dict[str, Any]:
    """Parse odds from a single Odds API event into structured dict.

    Returns:
        {
            "h2h": {bookmaker: {"Home": 1.75, "Draw": 3.80, "Away": 4.20}},
            "totals": {bookmaker: {"over": 1.85, "under": 2.00, "line": 2.5}},
            "best_odds": {"home": {"price": 1.78, "bookmaker": "1xBet"}, ...},
            "home_team": "...",
            "away_team": "...",
        }
    """
    result: Dict[str, Any] = {
        "h2h": {},
        "totals": {},
        "best_odds": {},
        "home_team": event.get("home_team", ""),
        "away_team": event.get("away_team", ""),
    }

    for bm in event.get("bookmakers", []):
        bm_title = bm.get("title", bm.get("key", ""))
        for market in bm.get("markets", []):
            mkey = market.get("key", "")

            if mkey == "h2h":
                outcomes = {}
                for o in market.get("outcomes", []):
                    outcomes[o["name"]] = o["price"]
                result["h2h"][bm_title] = outcomes

            elif mkey == "totals":
                for o in market.get("outcomes", []):
                    result["totals"].setdefault(bm_title, {})
                    result["totals"][bm_title][o["name"].lower()] = o["price"]
                    if "point" in o:
                        result["totals"][bm_title]["line"] = o["point"]

    # Find best odds for each outcome
    for outcome in ["Home", "Draw", "Away"]:
        best_price = 0.0
        best_bm = ""
        for bm, odds in result["h2h"].items():
            price = odds.get(outcome, 0)
            if price > best_price:
                best_price = price
                best_bm = bm
        if best_price > 0:
            result["best_odds"][outcome.lower()] = {
                "price": best_price,
                "bookmaker": best_bm,
            }

    return result


async def get_match_odds(
    sport_slug: str, home: str, away: str
) -> Optional[Dict[str, Any]]:
    """Get odds for a specific match by team names.

    Fetches all events for the sport, then finds the matching one.
    Returns parsed odds dict or None.
    """
    events = await fetch_odds_for_sport(sport_slug)
    if not events:
        return None

    for event in events:
        if _fuzzy_match_teams(event, home, away):
            return parse_event_odds(event)

    logger.debug("OddsAPI: no match found for %s vs %s (%s)", home, away, sport_slug)
    return None


def format_odds_table(odds: Dict[str, Any], home: str, away: str) -> str:
    """Format odds as a text table for Telegram message.

    Example:
        💰 Коэффициенты:
        Pinnacle  │ П1: 1.75 │ X: 3.80 │ П2: 4.20
        Bet365    │ П1: 1.70 │ X: 3.90 │ П2: 4.50
        🏆 Лучший КЭФ П1: 1.78 (1xBet)
    """
    h2h = odds.get("h2h", {})
    if not h2h:
        return ""

    lines = ["💰 Коэффициенты:"]

    for bm_name, outcomes in h2h.items():
        h = outcomes.get("Home", 0)
        d = outcomes.get("Draw", 0)
        a = outcomes.get("Away", 0)
        bm_short = bm_name[:10].ljust(10)
        parts = []
        if h:
            parts.append(f"П1: {h:.2f}")
        if d:
            parts.append(f"X: {d:.2f}")
        if a:
            parts.append(f"П2: {a:.2f}")
        if parts:
            lines.append(f"  {bm_short} │ {' │ '.join(parts)}")

    # Best odds
    best = odds.get("best_odds", {})
    best_home = best.get("home", {})
    if best_home.get("price"):
        lines.append(
            f"  🏆 Лучший КЭФ П1: {best_home['price']:.2f} ({best_home['bookmaker']})"
        )

    # Totals
    totals = odds.get("totals", {})
    if totals:
        # Use first bookmaker's totals (usually Pinnacle)
        for bm_name, tot in totals.items():
            line_val = tot.get("line", 2.5)
            over_val = tot.get("over", 0)
            under_val = tot.get("under", 0)
            if over_val > 0:
                lines.append(f"  📊 ТБ {line_val}: {over_val:.2f} | ТМ {line_val}: {under_val:.2f}")
                break

    return "\n".join(lines)


def format_odds_compact(odds: Dict[str, Any]) -> str:
    """Format compact odds for Hunter (one line).

    Example: П1: 1.75 | X: 3.80 | П2: 4.20
    """
    best = odds.get("best_odds", {})
    if not best:
        # Fallback to first bookmaker h2h
        h2h = odds.get("h2h", {})
        if h2h:
            first_bm = next(iter(h2h.values()))
            parts = []
            h = first_bm.get("Home", 0)
            d = first_bm.get("Draw", 0)
            a = first_bm.get("Away", 0)
            if h:
                parts.append(f"П1: {h:.2f}")
            if d:
                parts.append(f"X: {d:.2f}")
            if a:
                parts.append(f"П2: {a:.2f}")
            return " | ".join(parts) if parts else ""
        return ""

    parts = []
    if best.get("home", {}).get("price"):
        parts.append(f"П1: {best['home']['price']:.2f}")
    if best.get("draw", {}).get("price"):
        parts.append(f"X: {best['draw']['price']:.2f}")
    if best.get("away", {}).get("price"):
        parts.append(f"П2: {best['away']['price']:.2f}")
    return " | ".join(parts)
