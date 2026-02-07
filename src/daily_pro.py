# src/jobs/daily_pro.py
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Bot

from src.integrations.sport_api import SportAPIClient
from src.llm_client import analyze_with_llm_cached
from src.pro_db import get_all_pro_users

logger = logging.getLogger(__name__)
MSK = ZoneInfo("Europe/Moscow")


async def run_daily_pro(bot: Bot):
    """
    Daily Pro job:
    - формирует Охотник дня
    - рассылает PRO пользователям
    """

    today = datetime.now(MSK).date()
    api = SportAPIClient()

    matches = []
    for sport in ["ice-hockey", "football", "basketball"]:
        try:
            items = await api.matches_by_date(sport, today)
            matches.extend(items)
        except Exception:
            logger.exception("Failed to load matches for %s", sport)

    # --- фильтрация ---
    filtered = []
    for m in matches:
        league = (getattr(m, "league", "") or "").lower()
        status = (getattr(m, "status", "") or "").lower()
        odds = getattr(m, "odds_base", None)

        if status not in {"notstarted", "scheduled", "fixture"}:
            continue
        if not odds:
            continue
        if any(x in league for x in ["friendly", "women", "youth"]):
            continue

        filtered.append({
            "id": m.id,
            "title": m.title,
            "league": m.league,
            "sport": sport,
            "start_time": m.start_time,
        })

    if not filtered:
        logger.warning("Daily Pro: no matches after filtering")
        return

    # --- LLM ---
    prompt = _build_daily_prompt(filtered)

    analysis, _ = await analyze_with_llm_cached(
        prompt,
        cache_key=f"daily_pro:{today.isoformat()}",
        ttl_s=60 * 60 * 6,  # 6 часов
        schema="daily_pro"
    )

    text = _render_daily_message(analysis, today)

    # --- рассылка ---
    for user in get_all_pro_users():
        try:
            await bot.send_message(
                chat_id=user.tg_user_id,
                text=text,
            )
        except Exception:
            logger.exception("Failed to send Daily Pro to %s", user.tg_user_id)


def _build_daily_prompt(matches: list[dict]) -> str:
    lines = [
        "Ты спортивный аналитик.",
        "Подготовь аналитическую сводку дня.",
        "",
        "Ограничения:",
        "- без прогнозов",
        "- без советов",
        "- без слов: ставь, бери, гарантия",
        "",
        "Матчи на сегодня:",
    ]

    for m in matches:
        lines.append(
            f"- {m['title']} | {m['league']} | старт {m['start_time']}"
        )

    lines += [
        "",
        "Сформируй:",
        "1) Топ-3 события дня",
        "2) Экспресс дня (2–3 матча, без исходов)",
        "3) Риски дня",
        "",
        "В конце добавь дисклеймер.",
    ]

    return "\n".join(lines)


def _render_daily_message(analysis: dict, day) -> str:
    lines = [
        f"🎯 Охотник дня • {day.isoformat()} (МСК)",
        "",
    ]

    for section in ["top3", "express", "risks"]:
        if section in analysis:
            lines.append(analysis[section])

    lines.append("")
    lines.append("ℹ️ Аналитический материал. Не является рекомендацией.")
    return "\n".join(lines)
