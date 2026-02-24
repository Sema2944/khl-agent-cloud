# src/odds_tracker.py
"""
Background odds tracker: periodically fetches and stores bookmaker odds
for upcoming matches. Enables Line Movement analysis.

Runs every 2 hours via asyncio.create_task() in service.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlmodel import Session

from .db import engine
from .integrations.odds_api import (
    SPORT_KEY_MAP,
    _get_api_key,
    fetch_odds_for_sport,
    parse_event_odds,
)

logger = logging.getLogger(__name__)

TRACK_INTERVAL = 7200  # 2 hours in seconds
TRACKED_SPORTS = ["football", "ice-hockey", "basketball"]  # high-value sports


def save_odds_snapshot(
    match_id: str,
    sport: str,
    odds: Dict[str, Any],
) -> int:
    """Save a snapshot of odds to odds_history table.

    Stores h2h outcomes for each bookmaker.
    Returns number of rows inserted.
    """
    now = datetime.utcnow()
    count = 0

    try:
        with Session(engine) as s:
            # H2H odds
            for bm_name, outcomes in odds.get("h2h", {}).items():
                for outcome, price in outcomes.items():
                    if not isinstance(price, (int, float)) or price <= 1.0:
                        continue
                    s.exec(
                        text("""
                            INSERT INTO odds_history
                            (match_id, sport, bookmaker, market, outcome, price, recorded_at)
                            VALUES (:mid, :sport, :bm, 'h2h', :outcome, :price, :ts)
                        """),
                        params={
                            "mid": match_id,
                            "sport": sport,
                            "bm": bm_name,
                            "outcome": outcome,
                            "price": round(float(price), 3),
                            "ts": now,
                        },
                    )
                    count += 1

            # Totals odds
            for bm_name, tot in odds.get("totals", {}).items():
                line_val = tot.get("line")
                for outcome_name in ("over", "under"):
                    price = tot.get(outcome_name, 0)
                    if not isinstance(price, (int, float)) or price <= 1.0:
                        continue
                    s.exec(
                        text("""
                            INSERT INTO odds_history
                            (match_id, sport, bookmaker, market, outcome, line, price, recorded_at)
                            VALUES (:mid, :sport, :bm, 'totals', :outcome, :line, :price, :ts)
                        """),
                        params={
                            "mid": match_id,
                            "sport": sport,
                            "bm": bm_name,
                            "outcome": outcome_name,
                            "line": line_val,
                            "price": round(float(price), 3),
                            "ts": now,
                        },
                    )
                    count += 1

            s.commit()
    except Exception:
        logger.exception("save_odds_snapshot failed for match_id=%s", match_id)

    return count


def get_line_movement(
    match_id: str,
    bookmaker: str = "Pinnacle",
    market: str = "h2h",
) -> Dict[str, List[Dict[str, Any]]]:
    """Get historical odds movement for a match.

    Returns: {outcome: [{"price": 1.75, "time": datetime}, ...]}
    """
    try:
        with Session(engine) as s:
            rows = s.exec(
                text("""
                    SELECT outcome, price, recorded_at
                    FROM odds_history
                    WHERE match_id = :mid AND bookmaker = :bm AND market = :market
                    ORDER BY recorded_at ASC
                """),
                params={"mid": match_id, "bm": bookmaker, "market": market},
            ).all()

        movement: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            outcome = row[0]
            movement.setdefault(outcome, [])
            movement[outcome].append({
                "price": float(row[1]),
                "time": row[2],
            })
        return movement

    except Exception:
        logger.exception("get_line_movement failed for match_id=%s", match_id)
        return {}


def format_line_movement(
    movement: Dict[str, List[Dict[str, Any]]],
    home: str,
    away: str,
) -> str:
    """Format line movement for display in Telegram.

    Example:
        📈 Движение линии (Pinnacle):
          П1 Chelsea: 2.10 → 1.95 → 1.75 📉 (-0.35)
          X: 3.50 → 3.70 → 3.80 📈 (+0.30)
          П2 Man Utd: 3.80 → 4.00 → 4.20 📈 (+0.40)
          ⚠️ Сильное движение на П1!
    """
    if not movement:
        return ""

    lines = ["📈 Движение линии (Pinnacle):"]
    significant_move = False
    move_direction = ""

    outcome_names = {
        "Home": f"П1 {home}",
        "Draw": "X",
        "Away": f"П2 {away}",
    }

    for outcome in ["Home", "Draw", "Away"]:
        history = movement.get(outcome, [])
        if len(history) < 2:
            continue

        first = history[0]["price"]
        last = history[-1]["price"]
        diff = last - first

        if abs(diff) < 0.03:
            arrow = "➡️"
        elif diff < 0:
            arrow = "📉"
        else:
            arrow = "📈"

        name = outcome_names.get(outcome, outcome)
        # Show last 4 snapshots
        prices = " → ".join(f"{h['price']:.2f}" for h in history[-4:])
        lines.append(f"  {name}: {prices} {arrow} ({diff:+.2f})")

        # Detect significant movement
        if abs(diff) >= 0.15:
            significant_move = True
            if outcome == "Home" and diff < 0:
                move_direction = f"💡 Деньги идут на {home}"
            elif outcome == "Away" and diff < 0:
                move_direction = f"💡 Деньги идут на {away}"
            elif outcome == "Home" and diff > 0:
                move_direction = f"💡 Деньги идут на {away}"
            elif outcome == "Away" and diff > 0:
                move_direction = f"💡 Деньги идут на {home}"

    if significant_move:
        lines.append(f"\n⚠️ Сильное движение линии!")
        if move_direction:
            lines.append(move_direction)

    # Only return if we have actual movement data (at least one outcome with 2+ snapshots)
    if len(lines) <= 1:
        return ""

    return "\n".join(lines)


def get_line_movement_summary(match_id: str, home: str, away: str) -> str:
    """Convenience: fetch + format line movement for a match."""
    movement = get_line_movement(match_id)
    if not movement:
        return ""
    return format_line_movement(movement, home, away)


def get_odds_confidence_adjustment(match_id: str) -> float:
    """Calculate confidence adjustment based on line movement.

    Returns: float (-0.10 to +0.10)
    - Positive = line movement confirms popular direction
    - Negative = line movement is against expectations
    """
    movement = get_line_movement(match_id)
    if not movement:
        return 0.0

    home_hist = movement.get("Home", [])
    if len(home_hist) < 2:
        return 0.0

    first = home_hist[0]["price"]
    last = home_hist[-1]["price"]
    diff = last - first

    # Strong drop in home odds = money flowing to home = more confident in home
    if diff < -0.20:
        return 0.05  # boost confidence
    elif diff < -0.10:
        return 0.03
    elif diff > 0.20:
        return -0.05  # reduce confidence
    elif diff > 0.10:
        return -0.03

    return 0.0


async def _track_one_sport(sport_slug: str) -> int:
    """Fetch and store odds for one sport. Returns events saved."""
    try:
        events = await fetch_odds_for_sport(sport_slug)
        saved = 0
        for event in events:
            event_id = event.get("id", "")
            if not event_id:
                continue
            parsed = parse_event_odds(event)
            n = save_odds_snapshot(event_id, sport_slug, parsed)
            if n > 0:
                saved += 1
        return saved
    except Exception:
        logger.exception("Odds tracker: failed for %s", sport_slug)
        return 0


async def odds_tracking_loop() -> None:
    """Background loop: collect odds snapshots every 2 hours."""
    if (os.getenv("ODDS_TRACKING_ENABLED") or "false").strip().lower() != "true":
        logger.info("Odds tracking disabled (set ODDS_TRACKING_ENABLED=true to enable)")
        return

    # Wait for app to fully start
    await asyncio.sleep(60)

    while True:
        if not _get_api_key():
            logger.info("Odds tracker: ODDS_API_KEY not set, sleeping")
            await asyncio.sleep(TRACK_INTERVAL)
            continue

        try:
            total_saved = 0
            for sport in TRACKED_SPORTS:
                n = await _track_one_sport(sport)
                total_saved += n
                await asyncio.sleep(2)  # small delay between sports

            logger.info(
                "Odds tracker: saved snapshots for %d events across %d sports",
                total_saved, len(TRACKED_SPORTS),
            )
        except Exception:
            logger.exception("Odds tracker: loop error")

        await asyncio.sleep(TRACK_INTERVAL)
