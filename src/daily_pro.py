# src/daily_pro.py
"""
Daily Hunter v2: AI-powered top picks generator.
Pipeline: fetch → filter top leagues → deterministic score → enrich odds →
          AI analysis (top-10 only) → diversify → save to DB → broadcast.
Runs at 08:00 UTC via scheduler in service.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from datetime import datetime, date
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlmodel import Session

from .db import engine
from .integrations.sport_api import SportAPIClient
from .llm_client import analyze_with_llm_cached

logger = logging.getLogger(__name__)
MSK = ZoneInfo("Europe/Moscow")

HUNTER_SPORTS = ["ice-hockey", "football", "basketball", "tennis", "mma"]

# ---------------------------------------------------------------------------
# TOP LEAGUES — only matches from these leagues are eligible for Hunter
# ---------------------------------------------------------------------------
TOP_LEAGUES_KEYWORDS: Dict[str, List[str]] = {
    "football": [
        "premier league", "epl", "la liga", "serie a", "bundesliga",
        "ligue 1", "рпл", "rpl", "российская премьер",
        "champions league", "лига чемпионов",
        "europa league", "лига европы",
        "conference league",
    ],
    "ice-hockey": [
        "khl", "кхл", "nhl", "нхл", "shl", "liiga",
    ],
    "basketball": [
        "nba", "нба", "euroleague", "евролига",
    ],
    "tennis": [
        "atp", "wta", "grand slam", "australian open",
        "roland garros", "wimbledon", "us open",
    ],
    "mma": [
        "ufc",
    ],
}

SPORT_EMOJI = {
    "football": "⚽", "ice-hockey": "🏒", "basketball": "🏀",
    "tennis": "🎾", "mma": "🥊",
}


def _is_top_league(sport: str, league: str) -> bool:
    """Check if league name matches any top-league keyword for the sport."""
    lg = (league or "").lower()
    keywords = TOP_LEAGUES_KEYWORDS.get(sport, [])
    return any(kw in lg for kw in keywords)


def _safe_float(v: Any) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _extract_hour_msk(start_time: str) -> Optional[int]:
    """Extract hour (MSK) from start_time string like '19:30' or '2026-02-22T19:30:00'."""
    if not start_time:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})", start_time)
    if m:
        return int(m.group(1))
    return None


def _extract_moneyline(odds_raw: Any) -> Dict[str, Any]:
    """Extract home/draw/away odds from various API response formats."""
    if not odds_raw or not isinstance(odds_raw, dict):
        return {}

    result: Dict[str, Any] = {}

    # Format 1: {"home": 1.65, "draw": 3.80, "away": 5.20}
    for key_h in ("home", "1", "homeWin", "home_win"):
        v = _safe_float(odds_raw.get(key_h))
        if v > 1.0:
            result["home"] = round(v, 2)
            break

    for key_d in ("draw", "x", "X", "tie"):
        v = _safe_float(odds_raw.get(key_d))
        if v > 1.0:
            result["draw"] = round(v, 2)
            break

    for key_a in ("away", "2", "awayWin", "away_win"):
        v = _safe_float(odds_raw.get(key_a))
        if v > 1.0:
            result["away"] = round(v, 2)
            break

    # Format 2: nested {"moneyline": {"home": ..., "away": ...}}
    if not result:
        ml = odds_raw.get("moneyline") or odds_raw.get("1x2") or {}
        if isinstance(ml, dict):
            for key_h in ("home", "1"):
                v = _safe_float(ml.get(key_h))
                if v > 1.0:
                    result["home"] = round(v, 2)
                    break
            for key_d in ("draw", "x"):
                v = _safe_float(ml.get(key_d))
                if v > 1.0:
                    result["draw"] = round(v, 2)
                    break
            for key_a in ("away", "2"):
                v = _safe_float(ml.get(key_a))
                if v > 1.0:
                    result["away"] = round(v, 2)
                    break

    # Totals
    for key_t in ("total", "totals", "overUnder"):
        t = odds_raw.get(key_t)
        if isinstance(t, dict):
            line = _safe_float(t.get("line") or t.get("total") or t.get("value"))
            over = _safe_float(t.get("over"))
            under = _safe_float(t.get("under"))
            if line > 0 and over > 1.0:
                result["total_line"] = line
                result["total_over"] = round(over, 2)
                result["total_under"] = round(under, 2) if under > 1.0 else 0
            break

    return result


# ---------------------------------------------------------------------------
# AI PROMPT — concrete recommendation with odds
# ---------------------------------------------------------------------------
_HUNTER_ANALYSIS_PROMPT = """Ты — AI-аналитик спортивных событий для бота Betly.
Проанализируй матч и дай КОНКРЕТНУЮ рекомендацию.

Матч: {title}
Лига: {league}
Страна: {country}
Время: {start_time}
Коэффициенты: {odds_text}

ЗАДАЧА:
1. Дай одну КОНКРЕТНУЮ рекомендацию (исход или тотал или их комбинацию)
2. Объясни в 2-3 предложениях ПОЧЕМУ — конкретные факты, форма, статистика
3. Оцени уверенность (60-85%)

Верни JSON:
{{
  "confidence": число от 0.60 до 0.85,
  "recommendation": "П1 + ТБ 2.5" или "ТБ 4.5" или "П2" (краткая формулировка),
  "rec_odds": число (приблизительный коэффициент на рекомендацию, например 2.10),
  "summary": "2-3 предложения конкретного анализа с фактами"
}}

ВАЖНО:
- Без слов "ставь", "бери", "гарантия". Только аналитический материал.
- Если данных мало — снижай confidence.
- Отвечай на русском."""


# ---------------------------------------------------------------------------
# Fetch & Filter
# ---------------------------------------------------------------------------
async def _fetch_all_matches_today() -> List[Dict[str, Any]]:
    """Fetch matches across all hunter sports for today (MSK)."""
    today = datetime.now(MSK).date()
    all_matches: List[Dict[str, Any]] = []
    api = SportAPIClient()
    for sport in HUNTER_SPORTS:
        try:
            items = await api.matches_by_date(sport, today)
            for m in items:
                all_matches.append({
                    "match_id": str(m.id),
                    "sport_slug": getattr(m, "sport_slug", sport),
                    "title": getattr(m, "title", "") or "",
                    "league": getattr(m, "league", "") or "",
                    "country": getattr(m, "country", "") or "",
                    "start_time": str(getattr(m, "start_time", "") or ""),
                    "status": str(getattr(m, "status", "") or "").lower(),
                    "odds": getattr(m, "odds_base", None),
                })
        except Exception:
            logger.exception("Hunter fetch failed for %s", sport)
    return all_matches


def _filter_matches(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only scheduled matches from top leagues."""
    result = []
    for m in matches:
        status = m.get("status", "")
        league = (m.get("league") or "").lower()
        sport = m.get("sport_slug", "")

        # Only pre-match (not started)
        if status not in {"notstarted", "scheduled", "fixture", "ns", ""}:
            continue

        # Skip friendlies, women, youth
        if any(x in league for x in ["friendly", "women", "youth", "u18", "u20", "u21"]):
            continue

        # Top-league filter
        if not _is_top_league(sport, m.get("league", "")):
            continue

        result.append(m)

    # Fallback: if too few matches pass the top-league filter,
    # relax and include all non-friendly matches
    if len(result) < 6:
        logger.info("Hunter: only %d top-league matches, relaxing filter", len(result))
        relaxed = []
        for m in matches:
            status = m.get("status", "")
            league = (m.get("league") or "").lower()
            if status not in {"notstarted", "scheduled", "fixture", "ns", ""}:
                continue
            if any(x in league for x in ["friendly", "women", "youth", "u18", "u20", "u21"]):
                continue
            relaxed.append(m)
        return relaxed

    return result


# ---------------------------------------------------------------------------
# Deterministic scoring (no LLM, fast)
# ---------------------------------------------------------------------------
def _deterministic_score(match: Dict[str, Any]) -> float:
    """Score a match based on deterministic criteria (no LLM)."""
    score = 0.0
    sport = match.get("sport_slug", "")
    league = match.get("league", "")

    # 1. Top-league bonus (+30)
    if _is_top_league(sport, league):
        score += 30

    # 2. Has odds (+20)
    odds = match.get("odds")
    if odds and isinstance(odds, dict):
        score += 20

    # 3. Close odds = intrigue (+0-25)
    if odds and isinstance(odds, dict):
        oh = _safe_float(odds.get("home") or odds.get("1") or odds.get("homeWin"))
        oa = _safe_float(odds.get("away") or odds.get("2") or odds.get("awayWin"))
        if oh > 1.0 and oa > 1.0:
            diff = abs(oh - oa)
            if diff < 0.5:
                score += 25
            elif diff < 1.0:
                score += 15
            elif diff < 2.0:
                score += 10

    # 4. Evening match MSK (+5)
    hour = _extract_hour_msk(match.get("start_time", ""))
    if hour is not None and 17 <= hour <= 23:
        score += 5

    return score


# ---------------------------------------------------------------------------
# Odds enrichment for finalists
# ---------------------------------------------------------------------------
async def _enrich_odds(match: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch detailed odds if not already present."""
    odds = match.get("odds")
    if odds and isinstance(odds, dict):
        # Already have odds from matches_by_date
        match["odds_parsed"] = _extract_moneyline(odds)
        return match

    # Try to fetch from match_odds API
    try:
        api = SportAPIClient()
        snap = await api.match_odds(match["sport_slug"], match["match_id"])
        raw = snap.raw or {}
        match["odds"] = raw
        match["odds_parsed"] = _extract_moneyline(raw)
    except Exception:
        logger.debug("Hunter: odds fetch failed for %s", match.get("match_id"))
        match["odds_parsed"] = {}
    return match


def _format_odds_text(odds: Dict[str, Any]) -> str:
    """Format odds dict to human-readable string for AI prompt."""
    parts = []
    if odds.get("home"):
        parts.append(f"П1: {odds['home']}")
    if odds.get("draw"):
        parts.append(f"X: {odds['draw']}")
    if odds.get("away"):
        parts.append(f"П2: {odds['away']}")
    if odds.get("total_line") and odds.get("total_over"):
        parts.append(f"ТБ {odds['total_line']}: {odds['total_over']}")
    return " | ".join(parts) if parts else "нет данных"


# ---------------------------------------------------------------------------
# AI scoring with concrete recommendations
# ---------------------------------------------------------------------------
async def _ai_analyze_match(match: Dict[str, Any]) -> Dict[str, Any]:
    """Ask AI to analyze a match and give concrete recommendation."""
    odds_parsed = match.get("odds_parsed", {})
    odds_text = _format_odds_text(odds_parsed)

    prompt = _HUNTER_ANALYSIS_PROMPT.format(
        title=match.get("title", ""),
        league=match.get("league", ""),
        country=match.get("country", ""),
        start_time=match.get("start_time", ""),
        odds_text=odds_text,
    )
    cache_key = f"hunter:v2:{match['match_id']}"

    try:
        result, meta = await analyze_with_llm_cached(
            prompt,
            cache_key=cache_key,
            schema="ui_live",
            ttl_s=3600 * 6,
        )

        if isinstance(result, dict):
            confidence = min(0.85, max(0.0, _safe_float(result.get("confidence", 0))))
            recommendation = str(result.get("recommendation", ""))[:50]
            rec_odds = _safe_float(result.get("rec_odds", 0))
            summary = str(result.get("summary", ""))[:500]

            return {
                **match,
                "confidence": confidence,
                "recommendation": recommendation,
                "rec_odds": rec_odds,
                "analysis_text": summary,
            }

        return {**match, "confidence": 0.0, "analysis_text": "", "recommendation": "", "rec_odds": 0}

    except Exception:
        logger.exception("Hunter AI analysis failed for %s", match.get("match_id"))
        return {**match, "confidence": 0.0, "analysis_text": "", "recommendation": "", "rec_odds": 0}


# ---------------------------------------------------------------------------
# Express builder
# ---------------------------------------------------------------------------
def _build_express(top3: List[Dict[str, Any]], pick_date: date) -> Dict[str, Any]:
    """Build express pick from top-3 recommendations."""
    legs = []
    total_odds = 1.0

    for p in top3:
        rec = p.get("recommendation", "")
        odds = _safe_float(p.get("rec_odds", 0))
        title_short = (p.get("title") or "").split(" — ")[0][:15]
        if rec:
            legs.append(f"{rec} ({title_short})")
            if odds > 1.0:
                total_odds *= odds

    return {
        "match_id": f"express_{pick_date.isoformat()}",
        "sport_slug": "multi",
        "title": "Экспресс дня",
        "league": ", ".join(p.get("league", "")[:20] for p in top3),
        "confidence": min((p.get("confidence", 0) for p in top3), default=0),
        "analysis_text": " + ".join(legs),
        "recommendation": f"КЭФ {total_odds:.2f}" if total_odds > 1.0 else "",
        "start_time": "",
        "odds_parsed": {},
        "rec_odds": round(total_odds, 2) if total_odds > 1.0 else 0,
        "pick_type": "express",
    }


# ---------------------------------------------------------------------------
# Save picks to DB
# ---------------------------------------------------------------------------
def _save_picks(picks: List[Dict[str, Any]], pick_date: date) -> None:
    """Save top picks to daily_picks table (v2 with new columns)."""
    try:
        with Session(engine) as s:
            s.exec(
                text("DELETE FROM daily_picks WHERE pick_date = :d"),
                params={"d": pick_date.isoformat()},
            )
            s.commit()

            for p in picks:
                odds_json_str = ""
                odds_data = p.get("odds_parsed") or {}
                if odds_data:
                    try:
                        odds_json_str = json.dumps(odds_data, ensure_ascii=False)
                    except Exception:
                        odds_json_str = ""

                s.exec(
                    text("""
                        INSERT INTO daily_picks
                        (pick_date, match_id, sport_slug, title, league,
                         confidence, analysis_text, pick_type,
                         start_time, odds_json, recommendation, created_at)
                        VALUES (:d, :mid, :sport, :title, :league,
                                :conf, :txt, :ptype,
                                :stime, :ojson, :rec, NOW())
                    """),
                    params={
                        "d": pick_date.isoformat(),
                        "mid": p.get("match_id", ""),
                        "sport": p.get("sport_slug", ""),
                        "title": p.get("title", ""),
                        "league": p.get("league", ""),
                        "conf": p.get("confidence", 0.0),
                        "txt": p.get("analysis_text", ""),
                        "ptype": p.get("pick_type", "top3"),
                        "stime": p.get("start_time", ""),
                        "ojson": odds_json_str,
                        "rec": p.get("recommendation", ""),
                    },
                )
            s.commit()
            logger.info("Hunter: saved %d picks for %s", len(picks), pick_date)
    except Exception:
        logger.exception("Hunter: save_picks failed")


# ---------------------------------------------------------------------------
# Broadcast to PRO users (v2 format)
# ---------------------------------------------------------------------------
def _format_hunter_message(picks: List[Dict[str, Any]], pick_date: date) -> str:
    """Build the Hunter broadcast message in the new rich format."""
    top3 = [p for p in picks if p.get("pick_type") == "top3"]

    lines = [
        f"🎯 Охотник — Топ матчи дня",
        f"{pick_date.strftime('%d.%m.%Y')} | Подобрано AI",
        "",
    ]

    for i, p in enumerate(top3[:3], 1):
        conf = int(float(p.get("confidence", 0)) * 100)
        sport = p.get("sport_slug", "")
        emoji = SPORT_EMOJI.get(sport, "🏆")
        title = p.get("title", "Матч")
        league = p.get("league", "")
        start = (p.get("start_time", "") or "")[:5]  # HH:MM
        rec = p.get("recommendation", "")
        rec_odds = _safe_float(p.get("rec_odds", 0))
        summary = (p.get("analysis_text") or "")[:200]

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"{i}️⃣ {emoji} {title}")

        league_time = ""
        if league:
            league_time = f"   🏆 {league}"
        if start:
            league_time += f" | {start} MSK" if league_time else f"   {start} MSK"
        if league_time:
            lines.append(league_time)

        # Odds line
        odds_data = {}
        ojson = p.get("odds_json", "")
        if ojson:
            try:
                odds_data = json.loads(ojson)
            except Exception:
                pass
        if not odds_data:
            odds_data = p.get("odds_parsed", {})

        if odds_data:
            parts = []
            if odds_data.get("home"):
                parts.append(f"П1: {odds_data['home']}")
            if odds_data.get("draw"):
                parts.append(f"X: {odds_data['draw']}")
            if odds_data.get("away"):
                parts.append(f"П2: {odds_data['away']}")
            if parts:
                lines.append(f"   💰 {' | '.join(parts)}")

        # Recommendation
        if rec:
            rec_str = f"   🎯 {rec}"
            if rec_odds > 1.0:
                rec_str += f" (КЭФ {rec_odds:.2f})"
            lines.append(rec_str)

        # Summary
        if summary:
            lines.append(f"   💡 {summary}")

        lines.append(f"   ✅ Уверенность: {conf}%")
        lines.append("")

    # Express
    express = [p for p in picks if p.get("pick_type") == "express"]
    if express:
        ep = express[0]
        express_text = ep.get("analysis_text", "")
        express_odds = ep.get("recommendation", "")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
        header = "⚡ Экспресс дня"
        if express_odds:
            header += f" ({express_odds})"
        lines.append(header)
        if express_text:
            lines.append(f"  {express_text}")
        lines.append("")

    lines.append("ℹ️ Аналитический материал, не является прогнозом.")
    return "\n".join(lines)


async def _broadcast_to_pro_users(bot, picks: List[Dict[str, Any]], pick_date: date) -> None:
    """Send hunter picks to all PRO users and trial users."""
    try:
        with Session(engine) as s:
            rows = s.exec(
                text("""
                    SELECT tg_user_id FROM users
                    WHERE tg_user_id IS NOT NULL
                      AND (
                        (is_premium = TRUE AND (premium_until IS NULL OR premium_until > NOW()))
                        OR (trial_started_at IS NOT NULL AND trial_started_at > NOW() - INTERVAL '3 days')
                      )
                """)
            ).all()
            user_ids = [r[0] for r in rows if r[0]]
    except Exception:
        logger.exception("Hunter broadcast: failed to fetch users")
        return

    if not picks or not user_ids:
        return

    msg = _format_hunter_message(picks, pick_date)

    sent = 0
    for uid in user_ids:
        try:
            await bot.send_message(chat_id=uid, text=msg)
            sent += 1
        except Exception:
            logger.warning("Hunter broadcast failed for user %s", uid)

    logger.info("Hunter: broadcast sent to %d/%d users", sent, len(user_ids))

    # Check for expired trial users → send trial-ended message
    try:
        with Session(engine) as s:
            expired = s.exec(
                text("""
                    SELECT tg_user_id FROM users
                    WHERE trial_started_at IS NOT NULL
                      AND trial_started_at <= NOW() - INTERVAL '3 days'
                      AND trial_started_at > NOW() - INTERVAL '4 days'
                      AND is_premium = FALSE
                      AND tg_user_id IS NOT NULL
                """)
            ).all()
        for row in expired:
            uid = row[0]
            try:
                await bot.send_message(
                    chat_id=uid,
                    text=(
                        "Твой 3-дневный пробный период Охотника завершён.\n\n"
                        "Оформи PRO, чтобы получать подборки каждый день!\n"
                        "Нажми /start → 🌟 PRO"
                    ),
                )
            except Exception:
                pass
    except Exception:
        logger.exception("Hunter: trial expiry check failed")


# ---------------------------------------------------------------------------
# MAIN PIPELINE
# ---------------------------------------------------------------------------
async def run_daily_hunter(bot=None) -> None:
    """Main entry point for daily hunter job (v2)."""
    logger.info("Hunter v2: starting daily run")
    today = datetime.now(MSK).date()

    # 1. Fetch all matches
    matches = await _fetch_all_matches_today()
    logger.info("Hunter: fetched %d matches total", len(matches))

    # 2. Filter: only scheduled + top leagues
    filtered = _filter_matches(matches)
    logger.info("Hunter: %d matches after filtering", len(filtered))

    if not filtered:
        logger.warning("Hunter: no matches found after filtering")
        return

    # 3. Deterministic scoring (fast, no LLM)
    for m in filtered:
        m["det_score"] = _deterministic_score(m)

    filtered.sort(key=lambda x: x.get("det_score", 0), reverse=True)
    top_candidates = filtered[:10]
    logger.info("Hunter: top-10 candidates selected (scores: %s)",
                [f"{m.get('det_score', 0):.0f}" for m in top_candidates])

    # 4. Enrich odds for top candidates
    for m in top_candidates:
        await _enrich_odds(m)
        await asyncio.sleep(0.3)

    # 5. AI analysis for top candidates
    scored = []
    for m in top_candidates:
        result = await _ai_analyze_match(m)
        scored.append(result)
        await asyncio.sleep(0.5)  # rate limit

    # 6. Rank by AI confidence
    scored.sort(key=lambda x: x.get("confidence", 0), reverse=True)

    # 7. Diversify: max 2 per sport
    top3: List[Dict[str, Any]] = []
    sport_count: Dict[str, int] = defaultdict(int)

    for m in scored:
        sport = m.get("sport_slug", "")
        if sport_count[sport] >= 2:
            continue
        top3.append(m)
        sport_count[sport] += 1
        if len(top3) == 3:
            break

    if not top3:
        logger.warning("Hunter: no picks with confidence after AI analysis")
        return

    for p in top3:
        p["pick_type"] = "top3"

    logger.info("Hunter: top-3 selected: %s",
                [(p.get("title", "")[:30], f"{p.get('confidence', 0):.0%}") for p in top3])

    # 8. Build express
    picks = list(top3)
    if len(top3) >= 2:
        express = _build_express(top3, today)
        picks.append(express)

    # 9. Save to DB
    _save_picks(picks, today)

    # 10. Broadcast
    if bot:
        await _broadcast_to_pro_users(bot, picks, today)

    logger.info("Hunter v2: daily run complete (%d picks)", len(picks))
