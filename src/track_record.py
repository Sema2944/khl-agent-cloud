# src/track_record.py
"""
Track Record: automatic result checking for Hunter picks.
- Checks finished matches every 2 hours
- Determines win/loss based on recommendation vs actual score
- Calculates stats (winrate, ROI, streaks)
- Evening broadcast: results of the day to channel + PRO users
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlmodel import Session

from .db import engine
from .integrations.sport_api import SportAPIClient
from .pro_db import OWNER_IDS

logger = logging.getLogger(__name__)
MSK = ZoneInfo("Europe/Moscow")

CHANNEL_USERNAME = (os.getenv("CHANNEL_USERNAME") or "").strip()

SPORT_EMOJI = {
    "football": "\u26bd", "ice-hockey": "\U0001f3d2", "basketball": "\U0001f3c0",
    "tennis": "\U0001f3be", "mma": "\U0001f94a",
}

# Statuses that indicate a finished match
FINISHED_STATUSES = {
    "ft", "finished", "final", "ended", "aet", "pen",  # football/hockey
    "complete", "completed",
    "match finished", "game finished",
    "w/o", "retired", "ret", "walkover",  # tennis
}


# ---------------------------------------------------------------------------
# Score parsing
# ---------------------------------------------------------------------------
def _parse_score(score_str: str) -> Optional[Tuple[int, int]]:
    """Parse score string like '2:1', '3-2', '2 : 1' into (home, away).

    For tennis scores like '6:4 7:5 6:3', returns None (use _tennis_winner).
    """
    if not score_str:
        return None
    s = score_str.strip()

    # Simple score: "2:1" or "3-2" or "2 : 1"
    m = re.match(r"^\s*(\d+)\s*[:\-]\s*(\d+)\s*$", s)
    if m:
        return int(m.group(1)), int(m.group(2))

    # Score with extra info: "2:1 (1:0)" — take the main score
    m = re.match(r"^\s*(\d+)\s*[:\-]\s*(\d+)\s*\(", s)
    if m:
        return int(m.group(1)), int(m.group(2))

    return None


def _tennis_winner(score_str: str) -> Optional[str]:
    """Determine tennis winner from set scores like '6:4 7:5' or '2:6 6:3 7:6'.

    Returns 'home' or 'away' or None.
    """
    if not score_str:
        return None

    sets = re.findall(r"(\d+)\s*[:\-]\s*(\d+)", score_str)
    if len(sets) < 2:
        return None

    home_sets = 0
    away_sets = 0
    for h, a in sets:
        h, a = int(h), int(a)
        if h > a:
            home_sets += 1
        elif a > h:
            away_sets += 1

    if home_sets > away_sets:
        return "home"
    elif away_sets > home_sets:
        return "away"
    return None


# ---------------------------------------------------------------------------
# Check if recommendation won
# ---------------------------------------------------------------------------
def check_if_won(
    recommendation: str,
    score_home: int,
    score_away: int,
    *,
    total_line: Optional[float] = None,
) -> Optional[str]:
    """Check if a recommendation was correct.

    Returns 'win', 'loss', or None (if can't determine).
    """
    if not recommendation:
        return None

    rec = recommendation.upper().strip()

    # Handle combinations: "П1 + ТБ 2.5" → split by "+"
    parts = [p.strip() for p in rec.replace("И", "+").split("+")]

    results = []
    for part in parts:
        r = _check_single(part, score_home, score_away, total_line=total_line)
        if r is None:
            return None  # can't determine → skip
        results.append(r)

    if not results:
        return None

    # All parts must be True for a combo to win
    return "win" if all(results) else "loss"


def _check_single(
    rec_part: str,
    score_home: int,
    score_away: int,
    *,
    total_line: Optional[float] = None,
) -> Optional[bool]:
    """Check a single recommendation part. Returns True/False/None."""
    total = score_home + score_away

    # П1 (home win)
    if re.search(r"\bП1\b|P1\b|HOME\b|1X2.*1", rec_part):
        return score_home > score_away

    # П2 (away win)
    if re.search(r"\bП2\b|P2\b|AWAY\b", rec_part):
        return score_away > score_home

    # X / Ничья (draw)
    if re.match(r"^\s*X\s*$", rec_part) or "НИЧЬЯ" in rec_part:
        return score_home == score_away

    # ТБ N (total over)
    m = re.search(r"(?:ТБ|TB|ТОТАЛ\s*БОЛЬШЕ|OVER)\s*(\d+[.,]?\d*)", rec_part)
    if m:
        line = float(m.group(1).replace(",", "."))
        return total > line

    # ТМ N (total under)
    m = re.search(r"(?:ТМ|TU|ТОТАЛ\s*МЕНЬШЕ|UNDER)\s*(\d+[.,]?\d*)", rec_part)
    if m:
        line = float(m.group(1).replace(",", "."))
        return total < line

    # ОЗ / BTTS (both teams to score)
    if "ОЗ" in rec_part or "BTTS" in rec_part or "ОБЕ ЗАБЬЮТ" in rec_part:
        return score_home > 0 and score_away > 0

    # 1X (home or draw)
    if re.search(r"\b1X\b", rec_part):
        return score_home >= score_away

    # X2 (draw or away)
    if re.search(r"\bX2\b", rec_part):
        return score_away >= score_home

    # 12 (no draw — either team wins)
    if re.search(r"\b12\b", rec_part):
        return score_home != score_away

    return None


def calculate_profit(result: str, rec_odds: float) -> float:
    """Calculate profit for a pick (assuming stake = 1.0)."""
    if result == "win" and rec_odds > 1.0:
        return round(rec_odds - 1.0, 2)
    elif result == "loss":
        return -1.0
    return 0.0


# ---------------------------------------------------------------------------
# Main result checker
# ---------------------------------------------------------------------------
async def check_results() -> int:
    """Check results for pending Hunter picks. Returns number of picks settled."""
    api = SportAPIClient()
    settled = 0
    wins = 0
    losses = 0

    try:
        with Session(engine) as s:
            rows = s.exec(
                text("""
                    SELECT id, match_id, sport_slug, title, recommendation,
                           odds_json, pick_date
                    FROM daily_picks
                    WHERE result IS NULL
                      AND pick_type = 'top3'
                      AND pick_date >= (NOW() - INTERVAL '3 days')::date
                """)
            ).all()
    except Exception:
        logger.exception("Track Record: failed to fetch pending picks")
        return 0

    if not rows:
        logger.info("Track Record: no pending picks to check")
        return 0

    logger.info("Track Record: checking %d pending picks", len(rows))

    for row in rows:
        pick_id = row[0]
        match_id = str(row[1])
        sport_slug = str(row[2])
        title = str(row[3])
        recommendation = str(row[4] or "")
        odds_json = str(row[5] or "")
        pick_date = row[6]

        # Parse rec_odds from odds_json
        rec_odds = 0.0
        if odds_json:
            try:
                import json
                oj = json.loads(odds_json)
                # Try to infer rec_odds from recommendation
                rec_lower = recommendation.lower()
                if "п1" in rec_lower and oj.get("home"):
                    rec_odds = float(oj["home"])
                elif "п2" in rec_lower and oj.get("away"):
                    rec_odds = float(oj["away"])
                elif "x" == rec_lower.strip() and oj.get("draw"):
                    rec_odds = float(oj["draw"])
            except Exception:
                pass

        try:
            dto = await api.match_details(sport_slug, match_id)
        except Exception:
            logger.debug("Track Record: can't fetch %s/%s", sport_slug, match_id)
            continue

        status = (dto.status or "").strip().lower()
        score = (dto.score or "").strip()

        if status not in FINISHED_STATUSES:
            continue

        if not score:
            continue

        # Determine result
        result = None

        # Tennis: use set winner
        if sport_slug == "tennis":
            winner = _tennis_winner(score)
            if winner and recommendation:
                rec_upper = recommendation.upper()
                if "П1" in rec_upper:
                    result = "win" if winner == "home" else "loss"
                elif "П2" in rec_upper:
                    result = "win" if winner == "away" else "loss"
        else:
            # Standard sports: parse score
            parsed = _parse_score(score)
            if parsed:
                h, a = parsed
                result = check_if_won(recommendation, h, a)

        if result is None:
            logger.info("Track Record: can't determine result for %s (%s, score=%s, rec=%s)",
                        title[:30], sport_slug, score, recommendation)
            continue

        profit = calculate_profit(result, rec_odds)

        try:
            with Session(engine) as s:
                s.exec(
                    text("""
                        UPDATE daily_picks
                        SET result = :result,
                            final_score = :score,
                            profit = :profit,
                            checked_at = NOW()
                        WHERE id = :pid
                    """),
                    params={
                        "result": result,
                        "score": score,
                        "profit": profit,
                        "pid": pick_id,
                    },
                )
                s.commit()
            settled += 1
            if result == "win":
                wins += 1
            else:
                losses += 1
            logger.info("Track Record: %s %s — %s (score=%s, profit=%+.2f)",
                        result.upper(), title[:30], recommendation, score, profit)
        except Exception:
            logger.exception("Track Record: failed to update pick %d", pick_id)

    logger.info("Track Record: settled %d picks (%dW / %dL)", settled, wins, losses)
    return settled


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
def get_stats(days: int = 30) -> Dict[str, Any]:
    """Get Track Record stats for the given period."""
    try:
        with Session(engine) as s:
            rows = s.exec(
                text("""
                    SELECT result, profit, pick_date,
                           COALESCE(
                               (SELECT regexp_replace(odds_json, '.*', '')
                                FROM daily_picks dp2 WHERE dp2.id = daily_picks.id),
                               ''
                           ) as odds_json_raw
                    FROM daily_picks
                    WHERE result IS NOT NULL
                      AND pick_type = 'top3'
                      AND pick_date >= (NOW() - INTERVAL :days_str)::date
                    ORDER BY pick_date DESC, id DESC
                """),
                params={"days_str": f"{days} days"},
            ).all()
    except Exception:
        logger.exception("Track Record: failed to fetch stats")
        return _empty_stats()

    if not rows:
        return _empty_stats()

    total = len(rows)
    wins_count = sum(1 for r in rows if r[0] == "win")
    losses_count = sum(1 for r in rows if r[0] == "loss")
    winrate = (wins_count / total * 100) if total > 0 else 0.0
    pnl = sum(float(r[1] or 0) for r in rows)
    roi = (pnl / total * 100) if total > 0 else 0.0

    # Parse rec_odds for average
    total_odds = 0.0
    odds_count = 0
    for r in rows:
        try:
            import json
            oj = json.loads(r[3]) if r[3] else {}
            for key in ("home", "away", "draw"):
                v = oj.get(key)
                if v and float(v) > 1.0:
                    total_odds += float(v)
                    odds_count += 1
                    break
        except Exception:
            pass
    avg_odds = round(total_odds / odds_count, 2) if odds_count > 0 else 0.0

    # Streaks
    results_list = [r[0] for r in rows]  # newest first
    streak_current, streak_type = _current_streak(results_list)
    streak_best = _best_streak(results_list)

    # Recent 7 days
    seven_days_ago = (datetime.now(MSK).date() - timedelta(days=7))
    recent_7 = [r[0] for r in rows if r[2] and r[2] >= seven_days_ago]
    recent_7.reverse()  # oldest first for display

    # 7-day stats
    wins_7 = sum(1 for r in recent_7 if r == "win")
    total_7 = len(recent_7)
    winrate_7 = (wins_7 / total_7 * 100) if total_7 > 0 else 0.0

    return {
        "total": total,
        "wins": wins_count,
        "losses": losses_count,
        "winrate": round(winrate, 1),
        "roi": round(roi, 1),
        "pnl": round(pnl, 2),
        "avg_odds": avg_odds,
        "streak_current": streak_current,
        "streak_type": streak_type,
        "streak_best": streak_best,
        "recent_7": recent_7,
        "wins_7": wins_7,
        "total_7": total_7,
        "winrate_7": round(winrate_7, 1),
    }


def _empty_stats() -> Dict[str, Any]:
    return {
        "total": 0, "wins": 0, "losses": 0, "winrate": 0.0,
        "roi": 0.0, "pnl": 0.0, "avg_odds": 0.0,
        "streak_current": 0, "streak_type": "",
        "streak_best": 0, "recent_7": [],
        "wins_7": 0, "total_7": 0, "winrate_7": 0.0,
    }


def _current_streak(results: List[str]) -> Tuple[int, str]:
    """Get current streak from results (newest first)."""
    if not results:
        return 0, ""
    current = results[0]
    count = 0
    for r in results:
        if r == current:
            count += 1
        else:
            break
    return count, current


def _best_streak(results: List[str]) -> int:
    """Get best win streak from results."""
    best = 0
    current = 0
    for r in reversed(results):  # oldest first
        if r == "win":
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def format_stats_message(stats: Dict[str, Any]) -> str:
    """Format stats for /stats command."""
    if stats["total"] == 0:
        return (
            "\U0001f4ca Track Record \u041e\u0445\u043e\u0442\u043d\u0438\u043a\u0430\n\n"
            "\u041f\u043e\u043a\u0430 \u043d\u0435\u0442 \u0434\u0430\u043d\u043d\u044b\u0445 \u2014 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u044b \u043f\u043e\u044f\u0432\u044f\u0442\u0441\u044f \u043f\u043e\u0441\u043b\u0435 \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0438\u044f \u043c\u0430\u0442\u0447\u0435\u0439."
        )

    lines = [
        "\U0001f4ca Track Record \u041e\u0445\u043e\u0442\u043d\u0438\u043a\u0430",
        "",
    ]

    # Recent 7 days visual
    recent_7 = stats.get("recent_7", [])
    if recent_7:
        emoji_row = "".join("\u2705" if r == "win" else "\u274c" for r in recent_7)
        lines.append(f"\u041f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0435 7 \u0434\u043d\u0435\u0439:")
        lines.append(f"{emoji_row} \u2014 {stats['winrate_7']:.0f}% ({stats['wins_7']}/{stats['total_7']})")
        lines.append("")

    # 30-day stats
    lines.append(f"\u041f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0435 30 \u0434\u043d\u0435\u0439:")
    lines.append(f"\u0412\u0441\u0435\u0433\u043e \u043f\u0438\u043a\u043e\u0432: {stats['total']}")
    lines.append(f"\u041f\u043e\u0431\u0435\u0434: {stats['wins']} ({stats['winrate']:.0f}%)")
    if stats["avg_odds"] > 0:
        lines.append(f"\u0421\u0440\u0435\u0434\u043d\u0438\u0439 \u041a\u042d\u0424: {stats['avg_odds']:.2f}")
    lines.append(f"ROI: {stats['roi']:+.1f}%")
    lines.append("")

    # Streaks
    if stats["streak_best"] > 1:
        lines.append(f"\U0001f525 \u041b\u0443\u0447\u0448\u0430\u044f \u0441\u0435\u0440\u0438\u044f: {stats['streak_best']} \u043f\u043e\u0431\u0435\u0434 \u043f\u043e\u0434\u0440\u044f\u0434")
    if stats["streak_current"] > 0:
        streak_emoji = "\u2705" if stats["streak_type"] == "win" else "\u274c"
        lines.append(f"\U0001f4c8 \u0422\u0435\u043a\u0443\u0449\u0430\u044f \u0441\u0435\u0440\u0438\u044f: {stats['streak_current']} {streak_emoji}")

    return "\n".join(lines)


def format_evening_results(
    picks: List[Dict[str, Any]],
    pick_date: date,
    stats: Optional[Dict[str, Any]] = None,
) -> str:
    """Format evening results post for channel."""
    date_str = pick_date.strftime("%d %B").lstrip("0")
    # Russian month names
    months_ru = {
        "January": "\u044f\u043d\u0432\u0430\u0440\u044f", "February": "\u0444\u0435\u0432\u0440\u0430\u043b\u044f", "March": "\u043c\u0430\u0440\u0442\u0430",
        "April": "\u0430\u043f\u0440\u0435\u043b\u044f", "May": "\u043c\u0430\u044f", "June": "\u0438\u044e\u043d\u044f",
        "July": "\u0438\u044e\u043b\u044f", "August": "\u0430\u0432\u0433\u0443\u0441\u0442\u0430", "September": "\u0441\u0435\u043d\u0442\u044f\u0431\u0440\u044f",
        "October": "\u043e\u043a\u0442\u044f\u0431\u0440\u044f", "November": "\u043d\u043e\u044f\u0431\u0440\u044f", "December": "\u0434\u0435\u043a\u0430\u0431\u0440\u044f",
    }
    for en, ru in months_ru.items():
        date_str = date_str.replace(en, ru)

    lines = [
        f"\U0001f4ca \u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u044b \u041e\u0445\u043e\u0442\u043d\u0438\u043a\u0430 \u2014 {date_str}",
        "",
    ]

    wins_today = 0
    total_today = 0

    for i, p in enumerate(picks, 1):
        sport = p.get("sport_slug", "")
        emoji = SPORT_EMOJI.get(sport, "\U0001f3c6")
        title = p.get("title", "")
        score = p.get("final_score", "")
        result = p.get("result", "")
        rec = p.get("recommendation", "")
        profit = float(p.get("profit", 0) or 0)

        result_emoji = "\u2705" if result == "win" else "\u274c"
        total_today += 1
        if result == "win":
            wins_today += 1

        # Title with score: "Bayer 3:0 Olympiakos"
        title_parts = title.split(" \u2014 ", 1)
        if len(title_parts) == 2 and score:
            display = f"{title_parts[0]} {score} {title_parts[1]}"
        else:
            display = f"{title} ({score})" if score else title

        lines.append(f"{i}\ufe0f\u20e3 {emoji} {display} {result_emoji}")

        # Recommendation result
        if result == "win":
            rec_text = f"   {rec} \u0437\u0430\u0448\u0451\u043b!"
            if profit > 0:
                rec_text += f" \u041f\u0440\u043e\u0444\u0438\u0442: +{profit:.2f}"
            lines.append(rec_text)
        else:
            lines.append(f"   {rec} \u043d\u0435 \u0437\u0430\u0448\u0451\u043b")

        lines.append("")

    # Day summary
    result_emojis = "".join(
        "\u2705" if p.get("result") == "win" else "\u274c" for p in picks
    )
    lines.append(f"\u0418\u0442\u043e\u0433 \u0434\u043d\u044f: {wins_today}/{total_today} {result_emojis}")
    lines.append("")

    # Week / month stats
    if stats:
        if stats["total_7"] > 0:
            lines.append(f"\U0001f4c8 \u0417\u0430 \u043d\u0435\u0434\u0435\u043b\u044e: {stats['wins_7']}/{stats['total_7']} ({stats['winrate_7']:.0f}%)")
        if stats["total"] > 0:
            lines.append(f"\u0417\u0430 \u043c\u0435\u0441\u044f\u0446: {stats['wins']}/{stats['total']} ({stats['winrate']:.0f}%)")
        lines.append("")

    lines.append("\U0001f3af \u041f\u043e\u043b\u0443\u0447\u0430\u0439 \u043f\u0438\u043a\u0438 \u043f\u0435\u0440\u0432\u044b\u043c \u2192 @BetlyAIBot")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Evening broadcast
# ---------------------------------------------------------------------------
async def evening_results_broadcast(bot) -> bool:
    """Post evening results to channel and PRO users. Returns True if posted."""
    today = datetime.now(MSK).date()

    # Fetch today's settled picks
    try:
        with Session(engine) as s:
            rows = s.exec(
                text("""
                    SELECT id, match_id, sport_slug, title, recommendation,
                           result, final_score, profit, odds_json
                    FROM daily_picks
                    WHERE pick_date = :d
                      AND pick_type = 'top3'
                      AND result IS NOT NULL
                    ORDER BY id
                """),
                params={"d": today.isoformat()},
            ).all()
    except Exception:
        logger.exception("Evening broadcast: failed to fetch picks")
        return False

    if len(rows) < 2:
        logger.info("Evening broadcast: only %d settled picks, skipping", len(rows))
        return False

    picks = []
    for r in rows:
        picks.append({
            "sport_slug": r[2],
            "title": r[3],
            "recommendation": r[4],
            "result": r[5],
            "final_score": r[6],
            "profit": r[7],
            "odds_json": r[8],
        })

    stats = get_stats(days=30)
    msg = format_evening_results(picks, today, stats)

    # Post to channel
    if CHANNEL_USERNAME:
        try:
            await bot.send_message(chat_id=CHANNEL_USERNAME, text=msg)
            logger.info("Evening broadcast: posted to channel %s", CHANNEL_USERNAME)
        except Exception:
            logger.exception("Evening broadcast: channel post failed")

    # Send to PRO users + owners
    try:
        with Session(engine) as s:
            user_rows = s.exec(
                text("""
                    SELECT tg_user_id FROM users
                    WHERE tg_user_id IS NOT NULL
                      AND (
                        (is_premium = TRUE AND (premium_until IS NULL OR premium_until > NOW()))
                        OR (trial_started_at IS NOT NULL AND trial_started_at > NOW() - INTERVAL '3 days')
                      )
                """)
            ).all()
            user_ids = set(r[0] for r in user_rows if r[0])
    except Exception:
        logger.exception("Evening broadcast: failed to fetch users")
        user_ids = set()

    user_ids.update(OWNER_IDS)

    sent = 0
    for uid in user_ids:
        try:
            await bot.send_message(chat_id=uid, text=msg)
            sent += 1
        except Exception:
            logger.debug("Evening broadcast: failed for user %s", uid)

    logger.info("Evening broadcast: sent to %d/%d users", sent, len(user_ids))
    return True
