# src/daily_pro.py
"""
Daily Hunter: AI-powered top picks generator.
Pipeline: fetch matches → filter → AI score → rank → save to DB → broadcast.
Runs at 08:00 UTC via scheduler in service.py.
"""
from __future__ import annotations

import asyncio
import logging
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

HUNTER_SPORTS = ["ice-hockey", "football", "basketball"]

_SCORE_PROMPT = """Ты спортивный аналитик. Оцени матч по интересности для аналитического разбора.

Матч: {title}
Лига: {league}
Страна: {country}
Старт: {start_time}

Критерии интересности:
- Значимость матча (плей-офф > регулярка)
- Уровень команд
- Интрига (близкие силы)
- Движение линии (если известно)

Верни JSON:
{{
  "confidence": число от 0.0 до 1.0,
  "summary": "Краткий анализ (2-3 предложения, без рекомендаций и советов)"
}}

ВАЖНО: без слов "ставь", "бери", "гарантия". Только анализ."""


async def _fetch_all_matches_today() -> List[Dict[str, Any]]:
    """Fetch matches across all hunter sports for today (MSK)."""
    today = datetime.now(MSK).date()
    all_matches = []
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
    """Keep only scheduled matches worth scoring."""
    result = []
    for m in matches:
        status = m.get("status", "")
        league = (m.get("league") or "").lower()

        # Only pre-match (not started)
        if status not in {"notstarted", "scheduled", "fixture", "ns", ""}:
            continue

        # Skip friendlies, women, youth
        if any(x in league for x in ["friendly", "women", "youth", "u18", "u20", "u21"]):
            continue

        result.append(m)
    return result


async def _score_match(match: Dict[str, Any]) -> Dict[str, Any]:
    """Ask AI to score a match for interestingness."""
    prompt = _SCORE_PROMPT.format(
        title=match.get("title", ""),
        league=match.get("league", ""),
        country=match.get("country", ""),
        start_time=match.get("start_time", ""),
    )
    cache_key = f"hunter:score:{match['match_id']}"

    try:
        result, meta = await analyze_with_llm_cached(
            prompt,
            cache_key=cache_key,
            schema="ui_live",
            ttl_s=3600 * 6,
        )

        if isinstance(result, dict):
            confidence = 0.0
            # Try extracting confidence from various possible response formats
            if "confidence" in result:
                try:
                    confidence = float(result["confidence"])
                except (ValueError, TypeError):
                    pass

            summary = ""
            if "summary" in result:
                summary = str(result["summary"])
            elif "title" in result:
                summary = str(result.get("summary") or result.get("title") or "")

            return {
                **match,
                "confidence": min(1.0, max(0.0, confidence)),
                "analysis_text": summary[:500],
            }

        return {**match, "confidence": 0.0, "analysis_text": ""}

    except Exception:
        logger.exception("Hunter score failed for %s", match.get("match_id"))
        return {**match, "confidence": 0.0, "analysis_text": ""}


def _save_picks(picks: List[Dict[str, Any]], pick_date: date) -> None:
    """Save top picks to daily_picks table."""
    try:
        with Session(engine) as s:
            # Clear existing picks for today
            s.exec(
                text("DELETE FROM daily_picks WHERE pick_date = :d"),
                params={"d": pick_date.isoformat()},
            )
            s.commit()

            for p in picks:
                s.exec(
                    text("""
                        INSERT INTO daily_picks
                        (pick_date, match_id, sport_slug, title, league, confidence, analysis_text, pick_type, created_at)
                        VALUES (:d, :mid, :sport, :title, :league, :conf, :txt, :ptype, NOW())
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
                    },
                )
            s.commit()
            logger.info("Hunter: saved %d picks for %s", len(picks), pick_date)
    except Exception:
        logger.exception("Hunter: save_picks failed")


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

    top3 = [p for p in picks if p.get("pick_type") == "top3"]

    lines = [f"🎯 Охотник — {pick_date.isoformat()} (МСК)\n"]
    lines.append("🏆 ТОП-3 СОБЫТИЯ ДНЯ\n")

    for i, p in enumerate(top3[:3], 1):
        conf = int(float(p.get("confidence", 0)) * 100)
        title = p.get("title", "Матч")
        league = p.get("league", "")
        summary = (p.get("analysis_text") or "")[:200]

        lines.append(f"{i}️⃣ {title}")
        if league:
            lines.append(f"   {league}")
        lines.append(f"   Confidence: {conf}%")
        if summary:
            lines.append(f"   {summary}")
        lines.append("")

    # Express
    express = [p for p in picks if p.get("pick_type") == "express"]
    if express:
        ep = express[0]
        lines.append("⚡ ЭКСПРЕСС ДНЯ")
        lines.append(ep.get("analysis_text", "")[:300])
        lines.append("")

    lines.append("ℹ️ Аналитический материал, не является рекомендацией.")
    msg = "\n".join(lines)

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
                        "Оформи PRO, чтобы получать подборки каждый день! ⭐\n"
                        "Нажми /start → PRO режим"
                    ),
                )
            except Exception:
                pass
    except Exception:
        logger.exception("Hunter: trial expiry check failed")


async def run_daily_hunter(bot=None) -> None:
    """Main entry point for daily hunter job."""
    logger.info("Hunter: starting daily run")
    today = datetime.now(MSK).date()

    matches = await _fetch_all_matches_today()
    logger.info("Hunter: fetched %d matches total", len(matches))

    filtered = _filter_matches(matches)
    logger.info("Hunter: %d matches after filtering", len(filtered))

    if not filtered:
        logger.warning("Hunter: no matches found after filtering")
        return

    # Score each match (limit to 40 to stay within LLM limits)
    scored = []
    for m in filtered[:40]:
        result = await _score_match(m)
        scored.append(result)
        await asyncio.sleep(0.5)  # rate limit

    # Rank by confidence, take top 3
    scored.sort(key=lambda x: x.get("confidence", 0), reverse=True)
    top3 = scored[:3]
    for p in top3:
        p["pick_type"] = "top3"

    # Express: combine top matches
    picks = list(top3)
    if len(top3) >= 2:
        express_parts = []
        for p in top3:
            title = (p.get("title") or "").split(" — ")
            if len(title) == 2:
                express_parts.append(title[0].strip())
            else:
                express_parts.append(p.get("title", "")[:20])

        picks.append({
            "match_id": f"express_{today.isoformat()}",
            "sport_slug": "multi",
            "title": "Экспресс дня",
            "league": ", ".join(p.get("league", "") for p in top3[:3]),
            "confidence": min(p.get("confidence", 0) for p in top3[:3]),
            "analysis_text": f"Комбинация: {' + '.join(express_parts)}",
            "pick_type": "express",
        })

    _save_picks(picks, today)

    if bot:
        await _broadcast_to_pro_users(bot, picks, today)

    logger.info("Hunter: daily run complete (%d picks)", len(picks))
