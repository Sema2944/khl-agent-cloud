# src/telegram_bot/app.py
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from ..db import get_session
from ..pro_db import is_pro, OWNER_IDS
from ..ui_text import (
    MAIN_MENU_TEXT, ONBOARDING_WELCOME, ONBOARDING_HOW_IT_WORKS,
    HUNTER_FREE_TEXT, HUNTER_EXAMPLE_TEXT, HUNTER_NOT_READY_TEXT,
    ABOUT_TEXT, MENU_HINT_TEXT,
)
from ..user_access import allowed_sports_for_user
from ..user_store import get_user_seen_intro, set_user_seen_intro, get_or_create_user

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").strip()
WEBHOOK_PATH = "/telegram/webhook"
WEBHOOK_URL = (os.getenv("TELEGRAM_WEBHOOK_URL") or "").strip()

# feature flags
HIDE_LOCKED_SPORTS = (os.getenv("HIDE_LOCKED_SPORTS") or "0").strip() == "1"

MSK = datetime.now().astimezone().tzinfo

# ---------------------------------------------------------------------------
# Update deduplication (handler-level)
# ---------------------------------------------------------------------------
_processed_update_ids: set = set()
_PROCESSED_MAX = 200


def _is_duplicate(update_id: int) -> bool:
    """Return True if this update was already processed (skip it)."""
    if update_id in _processed_update_ids:
        logger.debug("Handler dedup: skipping duplicate update_id=%s", update_id)
        return True
    _processed_update_ids.add(update_id)
    if len(_processed_update_ids) > _PROCESSED_MAX:
        to_remove = sorted(_processed_update_ids)[:_PROCESSED_MAX // 2]
        _processed_update_ids.difference_update(to_remove)
    return False


async def _safe_edit_or_send(q, text: str, reply_markup=None, parse_mode=None):
    """Try to edit message; on 'not modified' silently skip, on other errors send new."""
    try:
        kwargs: dict = {"reply_markup": reply_markup}
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        await q.edit_message_text(text, **kwargs)
    except Exception as exc:
        err_msg = str(exc).lower()
        if "not modified" in err_msg or "message is not modified" in err_msg:
            return  # same content — do NOT send a duplicate
        # genuine error — fallback to reply
        try:
            kwargs2: dict = {"reply_markup": reply_markup}
            if parse_mode:
                kwargs2["parse_mode"] = parse_mode
            await q.message.reply_text(text, **kwargs2)
        except Exception:
            logger.warning("_safe_edit_or_send: both edit and reply failed")

# Telegram Application
_telegram_app: Optional[Application] = None

TG_TEXT_LIMIT = 3800

# Sports config from centralized source
try:
    from ..sports_config import get_sport_labels, get_default_sports
    SPORT_LABELS = get_sport_labels()
    DEFAULT_SPORTS = get_default_sports()
except Exception:
    SPORT_LABELS = {
        "ice-hockey": "🏒 Хоккей",
        "football": "⚽ Футбол",
        "basketball": "🏀 Баскетбол",
    }
    DEFAULT_SPORTS = ["ice-hockey", "football", "basketball"]


# ---------------------------------------------------------------------------
# Persistent reply keyboard (always visible at bottom of Telegram chat)
# ---------------------------------------------------------------------------
MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("⚽ Футбол"), KeyboardButton("🏀 Баскетбол")],
        [KeyboardButton("🏒 Хоккей"), KeyboardButton("🎾 Теннис")],
        [KeyboardButton("🥊 MMA"), KeyboardButton("🏐 Волейбол")],
        [KeyboardButton("🏎 Формула-1"), KeyboardButton("🔴 LIVE")],
        [KeyboardButton("🌟 PRO")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# Map reply keyboard button text → sport API slug
_REPLY_SPORT_MAP = {
    "⚽ Футбол": "football",
    "🏀 Баскетбол": "basketball",
    "🏒 Хоккей": "ice-hockey",
    "🎾 Теннис": "tennis",
    "🥊 MMA": "mma",
    "🏐 Волейбол": "volleyball",
    "🏎 Формула-1": "formula1",
}

# ---------------------------------------------------------------------------
# Global LIVE matches cache (shared across all users, 45s TTL)
# ---------------------------------------------------------------------------
_ALL_LIVE_CACHE: Optional[tuple] = None  # (timestamp, live_dict, upcoming_list)
_ALL_LIVE_TTL = 45

_LIVE_SPORTS = ["ice-hockey", "football", "basketball", "tennis", "mma"]

_LIVE_STATUS_KEYWORDS = {"live", "1h", "2h", "ht", "3h", "ot", "so", "in progress",
                          "inprogress", "in_progress", "playing", "p1", "p2", "p3",
                          "q1", "q2", "q3", "q4", "et", "bt", "s1", "s2", "s3",
                          "r1", "r2", "r3", "r4", "r5"}


def _is_live_status(status: str) -> bool:
    s = (status or "").strip().lower()
    return s in _LIVE_STATUS_KEYWORDS or "live" in s or "inprogress" in s.replace("_", "")


async def _fetch_all_live_matches() -> tuple:
    """Fetch all LIVE matches across all sports. Returns (by_sport, upcoming).
    Uses 45s global cache to avoid hammering API.
    """
    import time as _time
    global _ALL_LIVE_CACHE

    now = _time.time()
    if _ALL_LIVE_CACHE:
        ts, cached_live, cached_upcoming = _ALL_LIVE_CACHE
        if now - ts < _ALL_LIVE_TTL:
            return cached_live, cached_upcoming
        # TTL expired — release old data before fetching new
        _ALL_LIVE_CACHE = None

    from ..integrations.sport_api import SportAPIClient

    today = datetime.now(MSK).date()
    api = SportAPIClient()

    # Parallel fetch for all sports
    tasks = [api.matches_by_date(sport, today) for sport in _LIVE_SPORTS]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    by_sport: Dict[str, list] = {}
    all_upcoming: list = []

    for sport, result in zip(_LIVE_SPORTS, results):
        if isinstance(result, Exception):
            logger.warning("LIVE fetch failed for %s: %s", sport, result)
            continue
        if not result:
            continue

        live = []
        for m in result:
            status = str(getattr(m, "status", "") or "").strip()
            if _is_live_status(status):
                live.append(m)
            else:
                # Collect not-started for "upcoming" fallback (cap at 20 to save memory)
                s_lower = status.lower()
                if s_lower in {"notstarted", "ns", "not started", "scheduled", ""} and len(all_upcoming) < 20:
                    all_upcoming.append(m)

        if live:
            by_sport[sport] = live

    # Sort upcoming by start_time, take first 5
    all_upcoming.sort(key=lambda m: str(getattr(m, "start_time", "") or ""))
    upcoming_5 = all_upcoming[:5]

    _ALL_LIVE_CACHE = (now, by_sport, upcoming_5)
    return by_sport, upcoming_5


def _truncate_tg(text: str, limit: int = TG_TEXT_LIMIT) -> str:
    t = text or ""
    if len(t) <= limit:
        return t
    return t[: max(0, limit - 60)] + "\n\n…(сообщение обрезано)"


def _safe_markdown(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\\", "\\\\")
    s = s.replace("_", "\\_")
    s = s.replace("*", "\\*")
    s = s.replace("[", "\\[")
    s = s.replace("`", "\\`")
    return s


def _short_key(s: str, n: int = 10) -> str:
    h = hashlib.sha1((s or "").encode("utf-8")).hexdigest()
    return h[:n]


def _extract_hhmm(start_time: str) -> str:
    """Extract 'HH:MM' from any time format: '19:30', '2026-02-22T19:30:00', etc."""
    if not start_time:
        return ""
    m = re.search(r"(\d{1,2}):(\d{2})", str(start_time))
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return ""


def _truncate_at_sentence(text: str, limit: int = 200) -> str:
    """Truncate text at the last complete sentence within limit."""
    if not text or len(text) <= limit:
        return text
    chunk = text[:limit]
    for sep in [". ", "! ", "? "]:
        idx = chunk.rfind(sep)
        if idx > 0:
            return chunk[:idx + 1]
    idx = chunk.rfind(".")
    if idx > limit // 2:
        return chunk[:idx + 1]
    return chunk.rstrip() + "…"


def _normalize_score(raw: Any) -> str:
    """Normalize basketball dict-scores into readable format.

    '{'quarter_1': 35, 'total': 119}:{'quarter_1': 20, 'total': 98}'
    → '119:98 (35:20, 25:23, 40:21, 19:34)'
    """
    if raw is None:
        return ""
    if isinstance(raw, dict):
        total = raw.get("total")
        return str(total) if total is not None else ""
    s = str(raw).strip()
    if not s:
        return ""
    # Fast path: normal "X:Y" score
    if re.match(r'^\d+:\d+', s):
        return s
    # Detect stringified dicts: "{'quarter_1': ..., 'total': 119}:{'quarter_1': ..., 'total': 98}"
    if "quarter_" in s or "'total'" in s:
        try:
            import ast
            parts = re.split(r'\}:\{', s)
            if len(parts) == 2:
                home_d = ast.literal_eval(parts[0] + '}')
                away_d = ast.literal_eval('{' + parts[1])
                h_total = home_d.get('total')
                a_total = away_d.get('total')
                if h_total is not None and a_total is not None:
                    quarters = []
                    for q in range(1, 10):
                        h_q = home_d.get(f'quarter_{q}')
                        a_q = away_d.get(f'quarter_{q}')
                        if h_q is not None and a_q is not None:
                            quarters.append(f"{h_q}:{a_q}")
                    h_ot = home_d.get('over_time')
                    a_ot = away_d.get('over_time')
                    if h_ot is not None and a_ot is not None and (h_ot or a_ot):
                        quarters.append(f"{h_ot}:{a_ot}")
                    result = f"{h_total}:{a_total}"
                    if quarters:
                        result += f" ({', '.join(quarters)})"
                    return result
        except Exception:
            pass
    return s


def _compact_match_btn_title(title: str, score: str, status: str) -> str:
    t = (title or "").strip() or "Матч"
    sc = _normalize_score(score)
    st = (status or "").strip().lower()

    is_live = st in {"live", "inprogress", "in_progress", "ht"}
    is_done = any(x in st for x in ("finished", "ended", "ft", "final", "ret", "w/o"))
    is_ns = st in {"notstarted", "not_started", "scheduled", "ns", ""}
    is_postponed = st in {"postponed", "pst", "canc", "canceled", "cancelled"}

    prefix = ""
    if is_live:
        prefix = "🔴 "
    elif is_done:
        prefix = "✅ "
    elif is_postponed:
        prefix = "⏸ "
    elif is_ns:
        prefix = "⏳ "

    suffix = ""
    if (is_live or is_done) and sc:
        suffix = f"  {sc}"

    out = f"{prefix}{t}{suffix}".strip()
    if len(out) > 58:
        out = out[:57] + "…"
    return out


async def call_agent_local(user_id: int, text: str) -> str:
    """Вызываем локального агента (src/parsing.py)."""
    try:
        import importlib

        parsing_mod = importlib.import_module("src.parsing")
        fn = getattr(parsing_mod, "run_dialog_agent", None)
        if fn is None:
            return "⚠️ Агент не подключен: в src.parsing нет run_dialog_agent"

        return await fn(int(user_id), text)

    except Exception as e:
        logger.exception("call_agent_local failed")
        return f"⚠️ Агент временно недоступен: {type(e).__name__}: {str(e)[:160]}"


async def _get_odds_section(sport_slug: str, match_id: str, base_reply: str) -> str:
    """Build odds table + line movement section for PRO match analysis.

    Returns a text block to append to the match analysis, or empty string.
    """
    sections: list = []

    # 1. Multi-bookmaker odds from The Odds API
    try:
        from ..integrations.odds_api import get_match_odds, format_odds_table
        # Extract team names from base_reply (first line usually has "Team A — Team B")
        home = away = ""
        for line in base_reply.split("\n"):
            if " — " in line:
                parts = line.split(" — ", 1)
                home = re.sub(r"[^\w\s]", "", parts[0]).strip()
                away = re.sub(r"[^\w\s]", "", parts[1]).strip()
                if home and away:
                    break

        if home and away:
            odds = await get_match_odds(sport_slug, home, away)
            if odds and odds.get("h2h"):
                table = format_odds_table(odds, home, away)
                if table:
                    sections.append(table)
    except Exception:
        logger.debug("Odds table fetch failed for %s/%s", sport_slug, match_id)

    # 2. Line movement from odds_history
    try:
        from ..odds_tracker import get_line_movement_summary
        home2 = home or ""
        away2 = away or ""
        lm = get_line_movement_summary(match_id, home2, away2)
        if lm:
            sections.append(lm)
    except Exception:
        logger.debug("Line movement fetch failed for %s", match_id)

    if sections:
        return "\n\n" + "\n\n".join(sections)
    return ""


def _text_buy_pro(user_id: int) -> str:
    try:
        from .payments import tariff_text
        return tariff_text()
    except Exception:
        return (
            "🌟 Betly PRO — AI-аналитика спорта\n\n"
            "Что включено в PRO:\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🎯 Охотник — Топ-3 события дня каждое утро\n"
            "📊 Экспресс дня — собранный AI экспресс\n"
            "⚡ LIVE PRO — аналитика в реальном времени\n"
            "📈 Расширенная статистика и H2H\n"
            "🔔 Push-уведомления о результатах\n\n"
            "Тарифы:\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📅 Неделя — 299 ₽\n"
            "📅 Месяц — 799 ₽ (экономия 25%)\n"
            "📅 Сезон — 3 990 ₽ (экономия 17%)\n\n"
            "✅ Отмена в любой момент\n"
            "✅ Оплата картой прямо в Telegram"
        )


def _is_in_hunter_trial(user_id: int) -> bool:
    """Check if user is within their 3-day trial period."""
    try:
        from ..user_store import get_user_by_tg_id
        u = get_user_by_tg_id(user_id)
        if u is None:
            return False
        started = getattr(u, 'trial_started_at', None)
        if started is None:
            return False
        from datetime import timedelta, timezone
        now = datetime.now(timezone.utc)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return now < started + timedelta(days=3)
    except Exception:
        logger.exception("_is_in_hunter_trial failed")
        return False


def _get_today_picks() -> list:
    """Fetch today's daily picks from DB (v2 with odds, start_time, recommendation)."""
    try:
        from sqlmodel import Session as SMSession
        from sqlalchemy import text
        from zoneinfo import ZoneInfo
        from ..db import engine
        today = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()
        with SMSession(engine) as s:
            rows = s.exec(
                text(
                    "SELECT match_id, sport_slug, title, league, confidence, "
                    "analysis_text, pick_type, "
                    "COALESCE(start_time, ''), COALESCE(odds_json, ''), COALESCE(recommendation, '') "
                    "FROM daily_picks WHERE pick_date = :d ORDER BY pick_type, confidence DESC"
                ),
                params={"d": today},
            ).all()
            return [
                {
                    "match_id": r[0], "sport_slug": r[1], "title": r[2],
                    "league": r[3], "confidence": r[4], "analysis_text": r[5],
                    "pick_type": r[6],
                    "start_time": r[7] if len(r) > 7 else "",
                    "odds_json": r[8] if len(r) > 8 else "",
                    "recommendation": r[9] if len(r) > 9 else "",
                }
                for r in rows
            ]
    except Exception:
        logger.exception("_get_today_picks failed")
        return []


_HUNTER_SPORT_EMOJI = {
    "football": "⚽", "ice-hockey": "🏒", "basketball": "🏀",
    "tennis": "🎾", "mma": "🥊",
}


def _format_hunter_picks_text(picks: list, in_trial: bool = False) -> str:
    """Format hunter picks in the rich v2 format."""
    import json as _json
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    top3 = [p for p in picks if p.get("pick_type") == "top3"]
    express = [p for p in picks if p.get("pick_type") == "express"]

    today = _dt.now(_ZI("Europe/Moscow")).date()
    lines = [
        "🎯 Охотник — Топ матчи дня",
        f"{today.strftime('%d.%m.%Y')} | Подобрано AI",
        "",
    ]

    for i, p in enumerate(top3[:3], 1):
        conf = int(float(p.get("confidence", 0)) * 100)
        sport = p.get("sport_slug", "")
        emoji = _HUNTER_SPORT_EMOJI.get(sport, "🏆")
        title = (p.get("title") or "Матч")[:50]
        league = p.get("league", "")
        start = _extract_hhmm(p.get("start_time", ""))
        rec = p.get("recommendation", "")
        summary = _truncate_at_sentence(p.get("analysis_text") or "", 200)

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"{i}️⃣ {emoji} {title}")

        # League + time
        lt = ""
        if league:
            lt = f"   🏆 {league}"
        if start:
            lt += f" | {start} MSK" if lt else f"   {start} MSK"
        if lt:
            lines.append(lt)

        # Odds
        odds_data = {}
        ojson = p.get("odds_json", "")
        if ojson:
            try:
                odds_data = _json.loads(ojson)
            except Exception:
                pass
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
            lines.append(f"   🎯 {rec}")

        # Summary
        if summary:
            lines.append(f"   💡 {summary}")

        lines.append(f"   ✅ Уверенность: {conf}%")
        lines.append("")

    # Express
    if express:
        ep = express[0]
        express_text = ep.get("analysis_text", "")
        express_rec = ep.get("recommendation", "")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
        header = "⚡ Экспресс дня"
        if express_rec:
            header += f" ({express_rec})"
        lines.append(header)
        if express_text:
            lines.append(f"  {express_text}")
        lines.append("")

    if in_trial:
        lines.append("🎁 Пробный период (3 дня)")
        lines.append("")

    lines.append("ℹ️ Аналитический материал, не является прогнозом.")
    return "\n".join(lines)


async def _handle_hunter(q, user_id: int):
    """Show hunter screen — FREE or PRO version (v2 rich format)."""
    user_is_pro = False
    try:
        user_is_pro = is_pro(user_id)
    except Exception:
        logger.exception("is_pro check failed in hunter")

    in_trial = _is_in_hunter_trial(user_id)

    if user_is_pro or in_trial:
        picks = _get_today_picks()
        top3 = [p for p in picks if p.get("pick_type") == "top3"]

        if not top3:
            txt = HUNTER_NOT_READY_TEXT
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ В меню", callback_data="BACK:MENU")],
            ])
        else:
            txt = _format_hunter_picks_text(picks, in_trial=in_trial and not user_is_pro)
            rows = []
            for p in top3[:3]:
                title = (p.get("title") or "Матч")[:30]
                mid = p.get("match_id", "")
                sport = p.get("sport_slug", "ice-hockey")
                rows.append([InlineKeyboardButton(
                    f"🔍 {title}",
                    callback_data=f"MATCH:{sport}:{mid}"
                )])
            rows.append([InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")])
            kb = InlineKeyboardMarkup(rows)
    else:
        txt = HUNTER_FREE_TEXT
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Посмотреть пример", callback_data="HUNTER:EXAMPLE")],
            [InlineKeyboardButton("🌟 Оформить PRO", callback_data="MENU:PREMIUM")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="BACK:MENU")],
        ])

    await _safe_edit_or_send(q, _truncate_tg(txt), reply_markup=kb)


async def _handle_hunter_detail(q, user_id: int, match_id: str):
    """Show detailed analysis for a hunter pick, with bridge to match hub."""
    picks = _get_today_picks()
    pick = next((p for p in picks if p.get("match_id") == match_id), None)

    if not pick:
        txt = "Подборка не найдена."
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="MENU:HUNTER")],
        ])
    else:
        conf = int(float(pick.get("confidence", 0)) * 100)
        title = pick.get("title", "Матч")
        league = pick.get("league", "")
        analysis = pick.get("analysis_text", "")
        sport = pick.get("sport_slug", "ice-hockey")

        lines = [
            f"🎯 {title}",
            f"🏆 {league}" if league else "",
            f"Confidence: {conf}%",
            "",
            analysis or "Анализ недоступен.",
        ]
        txt = "\n".join(l for l in lines if l is not None)

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Открыть матч", callback_data=f"MATCH:{sport}:{match_id}")],
            [InlineKeyboardButton("⬅️ Назад к Охотнику", callback_data="MENU:HUNTER")],
        ])

    await _safe_edit_or_send(q, txt, reply_markup=kb)


def kb_main_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🎯 Охотник", callback_data="MENU:HUNTER")],
        [InlineKeyboardButton("📊 Анализ матчей", callback_data="MENU:MATCHES")],
        [InlineKeyboardButton("📈 Track Record", callback_data="MENU:STATS")],
        [InlineKeyboardButton("🌟 PRO", callback_data="MENU:PREMIUM")],
        [InlineKeyboardButton("👤 Профиль", callback_data="MENU:PROFILE")],
        [InlineKeyboardButton("ℹ️ О боте", callback_data="MENU:ABOUT")],
    ]
    return InlineKeyboardMarkup(rows)


def _is_allowed_sport(user_id: int, slug: str) -> bool:
    try:
        allowed = allowed_sports_for_user(int(user_id))
    except TypeError:
        # если вдруг старая сигнатура без user_id
        allowed = allowed_sports_for_user()  # type: ignore
    return (slug or "").strip().lower() in set((allowed or []))


def kb_sports(user_id: int) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for slug in DEFAULT_SPORTS:
        title = SPORT_LABELS.get(slug, slug)
        if _is_allowed_sport(user_id, slug):
            rows.append([InlineKeyboardButton(title, callback_data=f"SPORT:{slug}")])
        else:
            if HIDE_LOCKED_SPORTS:
                continue
            rows.append([InlineKeyboardButton(f"🔒 {title}", callback_data=f"SPORT_LOCKED:{slug}")])

    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="BACK:MENU")])
    return InlineKeyboardMarkup(rows)


def kb_match_hub(match_id: str) -> InlineKeyboardMarkup:
    """Клавиатура внутри матча: UI:<match_id>:<pre|live>:<action>"""
    mid = str(match_id).strip()
    rows: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("📊 PRE-обзор", callback_data=f"UI:{mid}:pre:overview"),
            InlineKeyboardButton("🟢 LIVE-обзор", callback_data=f"UI:{mid}:live:overview"),
        ],
        [
            InlineKeyboardButton("🟢 LIVE PRO", callback_data=f"UI:{mid}:live:pro"),
        ],
        [
            InlineKeyboardButton("🔄 Обновить LIVE", callback_data=f"UI:{mid}:live:refresh"),
        ],
        [
            InlineKeyboardButton("⬅️ Назад к матчам", callback_data="BACK:MATCHES"),
            InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


# ============================================================
# Navigation: Country -> League -> Matches (paged)
# ============================================================
@dataclass
class _NavState:
    sport: str
    today_iso: str
    country_by_key: Dict[str, str]
    league_by_key: Dict[Tuple[str, str], str]
    match_ids_by_league: Dict[Tuple[str, str], List[str]]
    match_meta: Dict[str, Dict[str, str]]
    last_screen: str  # "COUNTRIES" | "LEAGUES" | "MATCHES"
    last_ckey: str = ""
    last_lkey: str = ""
    last_page: int = 1


_NAV_BY_USER: Dict[int, _NavState] = {}
_PER_PAGE = 12


def _msk_today_iso() -> str:
    return datetime.now(MSK).date().isoformat()


def _league_ru(league: str) -> str:
    # Лигу "Other" можно оставить как запасной вариант, но UI "Other" по странам мы не используем
    return (league or "").strip() or "Other"


def _infer_country_from_league(league: str) -> str:
    """
    Если API не прислал страну — угадываем по лиге.
    ВАЖНО: MHL / VHL всегда относятся к России.
    """
    lg = (league or "").strip().lower()
    if not lg:
        return ""

    MAP = {
        # 🇷🇺 Россия
        "khl": "Russia",
        "вхл": "Russia",
        "vhl": "Russia",
        "мхл": "Russia",
        "mhl": "Russia",

        # 🇺🇸 США
        "nhl": "USA",
        "ahl": "USA",
        "echl": "USA",

        # Европа
        "shl": "Sweden",
        "liiga": "Finland",
        "del": "Germany",
        "national league": "Switzerland",
        "swiss league": "Switzerland",
        "extraliga": "Czech Republic",
        "tipsport": "Czech Republic",
        "slovak": "Slovakia",
        "icehl": "Austria",

        # 🌍 Международные
        "champions hockey league": "International",
        "chl": "International",
        "world championship": "International",
        "iihf": "International",
    }

    for key, country in MAP.items():
        if key in lg:
            return country

    return ""


def _country_title(country: str) -> str:
    """
    Отображение страны в UI (кнопки/заголовки).
    Россия — с флагом.
    International/пусто/Other — 🌍 Международные.
    Остальные — с флагами.
    """
    c = (country or "").strip()
    if (not c) or (c.lower() in {"other", "unknown", "none", "null", "n/a", "-"}):
        return "🌍 Международные"

    MAP = {
        "Russia": "🇷🇺 Россия",
        "Russian Federation": "🇷🇺 Россия",
        "USA": "🇺🇸 США",
        "United States": "🇺🇸 США",
        "Czech Republic": "🇨🇿 Чехия",
        "Czech": "🇨🇿 Чехия",
        "Finland": "🇫🇮 Финляндия",
        "Sweden": "🇸🇪 Швеция",
        "Germany": "🇩🇪 Германия",
        "Switzerland": "🇨🇭 Швейцария",
        "Slovakia": "🇸🇰 Словакия",
        "Austria": "🇦🇹 Австрия",
        "International": "🌍 Международные",
    }
    return MAP.get(c, c)


def _build_nav_state(user_id: int, sport_slug: str, matches: List[Any]) -> _NavState:
    _ = user_id
    today_iso = _msk_today_iso()

    country_by_key: Dict[str, str] = {}
    league_by_key: Dict[Tuple[str, str], str] = {}
    match_ids_by_league: Dict[Tuple[str, str], List[str]] = {}
    match_meta: Dict[str, Dict[str, str]] = {}

    for m in matches:
        mid = str(getattr(m, "id", "") or "").strip()
        if not mid:
            continue

        league_raw = (getattr(m, "league", "") or "").strip()
        league = _league_ru(league_raw)

        # "жадно" достаем страну из разных полей
        country_raw = (
            getattr(m, "country", "")
            or getattr(m, "league_country", "")
            or getattr(m, "leagueCountry", "")
            or getattr(m, "country_name", "")
            or ""
        ).strip()

        bad = (not country_raw) or (country_raw.lower() in {"other", "unknown", "none", "null", "n/a", "-"})
        if bad:
            country_raw = _infer_country_from_league(league_raw) or "International"

        # защита: если API дал International, но лига явно RU/USA — исправим (MHL/VHL сюда тоже попадают)
        if country_raw == "International":
            inferred = _infer_country_from_league(league_raw)
            if inferred:
                country_raw = inferred

        country = country_raw

        ckey = _short_key(country)
        lkey = _short_key(f"{country}::{league}")

        country_by_key[ckey] = country
        league_by_key[(ckey, lkey)] = league
        match_ids_by_league.setdefault((ckey, lkey), []).append(mid)

        match_meta[mid] = {
            "title": str(getattr(m, "title", "") or f"Матч {mid}"),
            "league": league,
            "country": country,
            "status": str(getattr(m, "status", "") or ""),
            "score": _normalize_score(getattr(m, "score", "") or ""),
            "start_time": str(getattr(m, "start_time", "") or ""),
        }

    for _key, ids in match_ids_by_league.items():
        def _sk(mid_: str) -> str:
            return (match_meta.get(mid_) or {}).get("start_time") or ""
        ids.sort(key=_sk)

    return _NavState(
        sport=sport_slug,
        today_iso=today_iso,
        country_by_key=country_by_key,
        league_by_key=league_by_key,
        match_ids_by_league=match_ids_by_league,
        match_meta=match_meta,
        last_screen="COUNTRIES",
        last_ckey="",
        last_lkey="",
        last_page=1,
    )


def _kb_countries(user_id: int, sport_slug: str) -> InlineKeyboardMarkup:
    st = _NAV_BY_USER.get(user_id)
    rows: List[List[InlineKeyboardButton]] = []
    if not st:
        rows.append([InlineKeyboardButton("⬅️ К спорту", callback_data="BACK:MATCHES_MENU")])
        return InlineKeyboardMarkup(rows)

    counts: List[Tuple[str, int]] = []
    for ckey in st.country_by_key.keys():
        n = 0
        for (ck, _lk), ids in st.match_ids_by_league.items():
            if ck == ckey:
                n += len(ids)
        counts.append((ckey, n))
    counts.sort(key=lambda x: x[1], reverse=True)

    buf: List[InlineKeyboardButton] = []
    for ckey, n in counts[:18]:
        cname_raw = st.country_by_key.get(ckey, "International")
        cname = _country_title(cname_raw)
        buf.append(InlineKeyboardButton(f"{cname} ({n})", callback_data=f"NAV:COUNTRY:{sport_slug}:{ckey}"))
        if len(buf) == 2:
            rows.append(buf)
            buf = []
    if buf:
        rows.append(buf)

    rows.append([InlineKeyboardButton("⬅️ К спорту", callback_data="BACK:MATCHES_MENU")])
    rows.append([InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")])
    return InlineKeyboardMarkup(rows)


def _kb_leagues(user_id: int, sport_slug: str, ckey: str) -> InlineKeyboardMarkup:
    st = _NAV_BY_USER.get(user_id)
    rows: List[List[InlineKeyboardButton]] = []
    if not st:
        rows.append([InlineKeyboardButton("⬅️ К спорту", callback_data="BACK:MATCHES_MENU")])
        return InlineKeyboardMarkup(rows)

    items: List[Tuple[str, int]] = []
    for (ck, lk), _lname in st.league_by_key.items():
        if ck != ckey:
            continue
        n = len(st.match_ids_by_league.get((ck, lk), []))
        items.append((lk, n))
    items.sort(key=lambda x: x[1], reverse=True)

    for lk, n in items[:30]:
        lname = st.league_by_key.get((ckey, lk), "Другое")
        rows.append([InlineKeyboardButton(f"{lname} ({n})", callback_data=f"NAV:LEAGUE:{sport_slug}:{ckey}:{lk}")])

    rows.append([InlineKeyboardButton("⬅️ Страны", callback_data=f"BACK:COUNTRIES:{sport_slug}")])
    rows.append([InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")])
    return InlineKeyboardMarkup(rows)


def _kb_matches(user_id: int, sport_slug: str, ckey: str, lkey: str, page: int) -> InlineKeyboardMarkup:
    st = _NAV_BY_USER.get(user_id)
    rows: List[List[InlineKeyboardButton]] = []
    if not st:
        rows.append([InlineKeyboardButton("⬅️ К спорту", callback_data="BACK:MATCHES_MENU")])
        return InlineKeyboardMarkup(rows)

    ids = st.match_ids_by_league.get((ckey, lkey), [])
    total = len(ids)
    pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)
    page = max(1, min(page, pages))

    start = (page - 1) * _PER_PAGE
    chunk = ids[start: start + _PER_PAGE]

    for mid in chunk:
        meta = st.match_meta.get(mid) or {}
        title = meta.get("title") or f"Матч {mid}"
        score = meta.get("score") or ""
        status = meta.get("status") or ""
        btn_title = _compact_match_btn_title(title, score, status)
        rows.append([InlineKeyboardButton(btn_title, callback_data=f"MATCH:{sport_slug}:{mid}")])

    nav_row: List[InlineKeyboardButton] = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"NAV:PAGE:{sport_slug}:{ckey}:{lkey}:{page-1}"))
    nav_row.append(InlineKeyboardButton(f"{page}/{pages}", callback_data="NOOP"))
    if page < pages:
        nav_row.append(InlineKeyboardButton("➡️", callback_data=f"NAV:PAGE:{sport_slug}:{ckey}:{lkey}:{page+1}"))
    rows.append(nav_row)

    rows.append([InlineKeyboardButton("⬅️ Лиги", callback_data=f"BACK:LEAGUES:{sport_slug}:{ckey}")])
    rows.append([InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")])
    return InlineKeyboardMarkup(rows)


def _text_countries(user_id: int, sport_slug: str) -> str:
    st = _NAV_BY_USER.get(user_id)
    title = SPORT_LABELS.get(sport_slug, sport_slug)
    if not st:
        return f"🏟 Матчи сегодня (по МСК) — {title}\nДата: {_msk_today_iso()}\n\nНет данных."
    return f"🏟 Матчи сегодня (по МСК) — {title}\nДата: {st.today_iso}\n\nВыбери страну:"


def _text_leagues(user_id: int, ckey: str) -> str:
    st = _NAV_BY_USER.get(user_id)
    if not st:
        return "Нет данных."
    country_raw = st.country_by_key.get(ckey, "International")
    country = _country_title(country_raw)
    return f"🏳️ Страна: {country}\n\nВыбери лигу:"


def _text_matches(user_id: int, ckey: str, lkey: str, page: int) -> str:
    st = _NAV_BY_USER.get(user_id)
    if not st:
        return "Нет данных."
    country_raw = st.country_by_key.get(ckey, "International")
    country = _country_title(country_raw)
    league = st.league_by_key.get((ckey, lkey), "Другое")
    ids = st.match_ids_by_league.get((ckey, lkey), [])
    total = len(ids)
    pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)
    page = max(1, min(page, pages))
    return (
        f"🏳️ {country}\n"
        f"🏆 {league}\n"
        f"Матчи: {total} • Страница {page}/{pages}\n\n"
        "Нажми матч ниже 👇"
    )


def kb_buy_pro() -> InlineKeyboardMarkup:
    try:
        from .payments import kb_tariffs
        return kb_tariffs()
    except Exception:
        rows = [
            [InlineKeyboardButton("⭐ Оформить Premium", callback_data="BUY:PRO")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="BACK:MENU")],
        ]
        return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# F1 custom navigation (races, not daily fixtures)
# ---------------------------------------------------------------------------
async def _render_f1_home(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    """Render F1 main screen: next race + action buttons."""
    from ..integrations.sport_api import SportAPIClient

    api = SportAPIClient()
    races = await api.f1_races_calendar(2026)

    # Find next upcoming race
    now = datetime.now(MSK)
    next_race = None
    for r in races:
        race_date_str = r.get("date", "")
        if not race_date_str:
            continue
        try:
            rd = datetime.fromisoformat(race_date_str.replace("Z", "+00:00"))
            if rd > now:
                next_race = r
                break
        except Exception:
            continue

    lines = ["🏎 Formula 1\n"]

    if next_race:
        comp = next_race.get("competition") or {}
        name = comp.get("name") or next_race.get("competition", {}).get("name", "Гран-при")
        loc = comp.get("location") or {}
        city = loc.get("city", "")
        country = loc.get("country", "")
        circuit = next_race.get("circuit") or {}
        circuit_name = circuit.get("name", "")

        race_date_str = next_race.get("date", "")
        date_display = ""
        if race_date_str:
            try:
                rd = datetime.fromisoformat(race_date_str.replace("Z", "+00:00"))
                date_display = rd.strftime("%d.%m.%Y %H:%M MSK")
            except Exception:
                date_display = race_date_str[:16]

        lines.append(f"📅 Ближайший этап:")
        lines.append(f"  🏁 {name}")
        if circuit_name:
            lines.append(f"  📍 {circuit_name}")
        if city or country:
            lines.append(f"  🌍 {city}, {country}" if city else f"  🌍 {country}")
        if date_display:
            lines.append(f"  📆 {date_display}")
    else:
        if races:
            lines.append("Сезон 2026: все гонки завершены")
        else:
            lines.append("Календарь сезона 2026 пока недоступен")

    text = "\n".join(lines)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Чемпионат пилотов", callback_data="F1:DRIVERS")],
        [InlineKeyboardButton("🏗 Чемпионат конструкторов", callback_data="F1:TEAMS")],
        [InlineKeyboardButton("📅 Календарь сезона", callback_data="F1:CALENDAR")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="BACK:MENU")],
    ])
    return text, kb


async def _render_f1_drivers(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    from ..integrations.sport_api import SportAPIClient
    api = SportAPIClient()
    standings = await api.f1_driver_standings(2026)

    lines = ["🏎 Чемпионат пилотов 2026\n"]
    if not standings:
        lines.append("Данные пока недоступны (сезон не начался)")
    else:
        for i, entry in enumerate(standings[:15], 1):
            driver = entry.get("driver") or {}
            name = driver.get("name", "?")
            team = (entry.get("team") or {}).get("name", "")
            points = entry.get("points", 0)
            pos_emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            line = f"  {pos_emoji} {name}"
            if team:
                line += f" ({team})"
            line += f" — {points} очк."
            lines.append(line)

    text = "\n".join(lines)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏗 Конструкторы", callback_data="F1:TEAMS")],
        [InlineKeyboardButton("⬅️ F1", callback_data="F1:HOME")],
    ])
    return text, kb


async def _render_f1_teams(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    from ..integrations.sport_api import SportAPIClient
    api = SportAPIClient()
    standings = await api.f1_team_standings(2026)

    lines = ["🏎 Чемпионат конструкторов 2026\n"]
    if not standings:
        lines.append("Данные пока недоступны (сезон не начался)")
    else:
        for i, entry in enumerate(standings[:10], 1):
            team = entry.get("team") or {}
            name = team.get("name", "?")
            points = entry.get("points", 0)
            pos_emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            lines.append(f"  {pos_emoji} {name} — {points} очк.")

    text = "\n".join(lines)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Пилоты", callback_data="F1:DRIVERS")],
        [InlineKeyboardButton("⬅️ F1", callback_data="F1:HOME")],
    ])
    return text, kb


async def _render_f1_calendar(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    from ..integrations.sport_api import SportAPIClient
    api = SportAPIClient()
    races = await api.f1_races_calendar(2026)

    lines = ["🏎 Календарь F1 2026\n"]
    if not races:
        lines.append("Календарь пока недоступен")
    else:
        now = datetime.now(MSK)
        for r in races:
            comp = r.get("competition") or {}
            name = comp.get("name", "Гран-при")
            race_date_str = r.get("date", "")
            date_short = ""
            is_past = False
            if race_date_str:
                try:
                    rd = datetime.fromisoformat(race_date_str.replace("Z", "+00:00"))
                    date_short = rd.strftime("%d.%m")
                    is_past = rd < now
                except Exception:
                    date_short = race_date_str[:5]
            marker = "✅" if is_past else "📅"
            lines.append(f"  {marker} {date_short} — {name}")

    text = "\n".join(lines)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ F1", callback_data="F1:HOME")],
    ])
    return text, kb


async def _render_sport_nav_root(user_id: int, sport_slug: str) -> Tuple[str, InlineKeyboardMarkup]:
    from ..integrations.sport_api import SportAPIClient, SportAPIError

    today = datetime.now(MSK).date()
    title = SPORT_LABELS.get(sport_slug, sport_slug)

    try:
        api = SportAPIClient()
        matches = await api.matches_by_date(sport_slug, today)
    except SportAPIError as e:
        logger.error("_render_sport_nav_root %s: %s", sport_slug, e)
        # Alert admin with full error; show users a clean message only
        try:
            from ..alerting import send_alert
            import asyncio
            asyncio.ensure_future(send_alert(
                "ERROR", f"sport_nav.{sport_slug}", e,
                context={"sport": sport_slug, "date": today.isoformat()},
            ))
        except Exception:
            pass
        text = (
            f"🏟 {title}\n"
            f"Дата: {today.isoformat()}\n\n"
            "⚠️ Данные по этому спорту временно недоступны.\n"
            "Попробуйте другой спорт или вернитесь позже."
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ К спорту", callback_data="BACK:MATCHES_MENU")]])
        return text, kb

    if not matches:
        extra = ""
        # For MMA: show next upcoming UFC event
        if sport_slug == "mma":
            try:
                from ..integrations.sport_api import SportAPIClient
                _api = SportAPIClient()
                import asyncio
                next_ev = await _api.fetch_next_ufc_event()
                if next_ev:
                    ev_name = next_ev.get("name", "UFC")
                    ev_date = next_ev.get("date", "")
                    # Parse date for display
                    if ev_date:
                        try:
                            from datetime import datetime as _dt
                            dt = _dt.fromisoformat(ev_date.replace("Z", "+00:00"))
                            ev_date_str = dt.strftime("%d.%m.%Y")
                        except Exception:
                            ev_date_str = ev_date[:10]
                        extra = f"\n\n🥊 Ближайший ивент: {ev_name}\n📅 {ev_date_str}"
            except Exception:
                pass
        text = (
            f"🏟 Матчи сегодня (по МСК) — {title}\n"
            f"Дата: {today.isoformat()}\n\n"
            "Сегодня нет запланированных матчей в доступных лигах.\n"
            f"Попробуйте завтра или выберите другой спорт.{extra}"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ К спорту", callback_data="BACK:MATCHES_MENU")]])
        return text, kb

    st = _build_nav_state(user_id, sport_slug, matches)
    _NAV_BY_USER[user_id] = st
    return _text_countries(user_id, sport_slug), _kb_countries(user_id, sport_slug)


def _nav_back_to_last(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    st = _NAV_BY_USER.get(user_id)
    if not st:
        return "🏟 Выбери спорт:", kb_sports(user_id)

    sport = st.sport
    if st.last_screen == "COUNTRIES":
        return _text_countries(user_id, sport), _kb_countries(user_id, sport)

    if st.last_screen == "LEAGUES" and st.last_ckey:
        return _text_leagues(user_id, st.last_ckey), _kb_leagues(user_id, sport, st.last_ckey)

    if st.last_screen == "MATCHES" and st.last_ckey and st.last_lkey:
        return (
            _text_matches(user_id, st.last_ckey, st.last_lkey, st.last_page),
            _kb_matches(user_id, sport, st.last_ckey, st.last_lkey, st.last_page),
        )

    return _text_countries(user_id, sport), _kb_countries(user_id, sport)


# ============================================================
# Handlers
# ============================================================
async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if _is_duplicate(update.update_id):
        return
    user_id = update.effective_user.id if update.effective_user else 0

    # Ensure user exists in DB
    try:
        get_or_create_user(
            user_id,
            username=getattr(update.effective_user, 'username', None),
            first_name=getattr(update.effective_user, 'first_name', None),
            last_name=getattr(update.effective_user, 'last_name', None),
        )
    except Exception:
        logger.exception("handle_start: get_or_create_user failed")

    # Referral deep link: /start ref_<user_id>
    args = context.args
    if args and len(args) > 0 and args[0].startswith("ref_"):
        try:
            referrer_id = int(args[0].replace("ref_", ""))
            if referrer_id != user_id:
                from .payments import process_referral
                bot = context.bot
                asyncio.create_task(process_referral(user_id, referrer_id, bot=bot))
        except (ValueError, TypeError):
            pass
        except Exception:
            logger.exception("handle_start: referral processing failed")

    # Onboarding check
    seen = True
    try:
        seen = get_user_seen_intro(user_id)
    except Exception:
        logger.exception("handle_start: onboarding check failed")

    if seen:
        await update.message.reply_text(
            "Выбери спорт 👇\n\n"
            "Или используй меню:\n"
            "🎯 /hunter — топ матчи дня\n"
            "🌟 /pro — PRO подписка\n"
            "🎁 Есть промокод? Введи /promo КОД",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    # Show onboarding for new users
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Начать", callback_data="ONBOARD:START")],
        [InlineKeyboardButton("ℹ️ Как это работает", callback_data="ONBOARD:HELP")],
    ])
    await update.message.reply_text(ONBOARDING_WELCOME, reply_markup=kb)


async def handle_pro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /pro — показать экран PRO-подписки."""
    if not update.message:
        return
    txt = _text_buy_pro(update.effective_user.id if update.effective_user else 0)
    await update.message.reply_text(txt, reply_markup=kb_buy_pro())


async def handle_hunter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /hunter — показать экран Охотника (v2 rich format)."""
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    user_is_pro = False
    try:
        user_is_pro = is_pro(user_id)
    except Exception:
        pass
    in_trial = _is_in_hunter_trial(user_id)

    if user_is_pro or in_trial:
        picks = _get_today_picks()
        top3 = [p for p in picks if p.get("pick_type") == "top3"]
        if not top3:
            txt = HUNTER_NOT_READY_TEXT
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ В меню", callback_data="BACK:MENU")],
            ])
        else:
            txt = _format_hunter_picks_text(picks, in_trial=in_trial and not user_is_pro)
            rows = []
            for p in top3[:3]:
                title = (p.get("title") or "Матч")[:30]
                mid = p.get("match_id", "")
                sport = p.get("sport_slug", "ice-hockey")
                rows.append([InlineKeyboardButton(
                    f"🔍 {title}",
                    callback_data=f"MATCH:{sport}:{mid}"
                )])
            if user_id in OWNER_IDS:
                rows.append([InlineKeyboardButton("🔄 Перегенерировать", callback_data="HUNTER:REFRESH")])
            rows.append([InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")])
            kb = InlineKeyboardMarkup(rows)
    else:
        txt = HUNTER_FREE_TEXT
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Посмотреть пример", callback_data="HUNTER:EXAMPLE")],
            [InlineKeyboardButton("🌟 Оформить PRO", callback_data="MENU:PREMIUM")],
            [InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")],
        ])
    await update.message.reply_text(_truncate_tg(txt), reply_markup=kb)


async def handle_hunter_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /hunter_refresh — принудительная перегенерация (только OWNER_IDS)."""
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else 0

    if user_id not in OWNER_IDS:
        await update.message.reply_text("⛔ Команда доступна только администратору.")
        return

    await update.message.reply_text("🔄 Перегенерирую Охотника... Это займёт 1-2 минуты.")

    try:
        from ..daily_pro import run_daily_hunter
        await run_daily_hunter(bot=context.bot)
    except Exception:
        logger.exception("hunter_refresh failed")
        await update.message.reply_text("❌ Ошибка при генерации. Смотри логи.")
        return

    # Show fresh picks
    picks = _get_today_picks()
    top3 = [p for p in picks if p.get("pick_type") == "top3"]
    if not top3:
        await update.message.reply_text("⚠️ Пайплайн отработал, но пиков нет (нет подходящих матчей).")
        return

    txt = _format_hunter_picks_text(picks)
    rows = []
    for p in top3[:3]:
        title = (p.get("title") or "Матч")[:30]
        mid = p.get("match_id", "")
        sport = p.get("sport_slug", "ice-hockey")
        rows.append([InlineKeyboardButton(
            f"🔍 {title}",
            callback_data=f"MATCH:{sport}:{mid}"
        )])
    rows.append([InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")])
    await update.message.reply_text(
        f"✅ Охотник перегенерирован!\n\n{_truncate_tg(txt)}",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def handle_hunter_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /hunter_status — статус Охотника (только OWNER_IDS)."""
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else 0

    if user_id not in OWNER_IDS:
        await update.message.reply_text("⛔ Команда доступна только администратору.")
        return

    from ..daily_pro import get_hunter_status
    info = get_hunter_status()

    last_run = info.get("last_run_at")
    if last_run:
        last_run_txt = last_run.strftime("%d.%m.%Y %H:%M MSK")
    else:
        last_run_txt = "никогда"

    status_map = {
        "never_run": "⚪ Не запускался",
        "running": "🟡 Запущен сейчас",
        "done": "🟢 Завершён",
        "error": "🔴 Ошибка",
    }
    status_txt = status_map.get(info.get("status", ""), info.get("status", "?"))

    picks_count = info.get("picks_count", 0)
    users_sent = info.get("users_sent", 0)

    # Next run: 08:00 UTC
    from datetime import timezone, timedelta
    now_utc = datetime.now(timezone.utc)
    target = now_utc.replace(hour=8, minute=0, second=0, microsecond=0)
    if now_utc >= target:
        target += timedelta(days=1)
    next_run_seconds = int((target - now_utc).total_seconds())
    next_run_hours = next_run_seconds // 3600
    next_run_mins = (next_run_seconds % 3600) // 60

    # Today's picks from DB
    picks_in_db = _get_today_picks()
    db_top3 = [p for p in picks_in_db if p.get("pick_type") == "top3"]

    lines = [
        "📊 Hunter Status",
        "",
        f"Статус: {status_txt}",
        f"Последний запуск: {last_run_txt}",
        f"Пиков сгенерировано: {picks_count}",
        f"Пиков в БД (сегодня): {len(db_top3)}",
        f"Юзеров получили рассылку: {users_sent}",
        "",
        f"Следующий запуск: через {next_run_hours}ч {next_run_mins}мин (08:00 UTC)",
    ]

    error = info.get("error")
    if error:
        lines.append(f"\n❌ Ошибка: {str(error)[:200]}")

    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Scheduled channel posts — admin commands (OWNER_IDS only)
# ---------------------------------------------------------------------------

async def handle_post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/post TEXT — publish text to channel immediately."""
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    if user_id not in OWNER_IDS:
        await update.message.reply_text("⛔ Команда доступна только администратору.")
        return

    post_text = " ".join(context.args) if context.args else ""
    if not post_text:
        await update.message.reply_text("Использование: /post <текст>")
        return

    from ..scheduled_posts import CHANNEL_USERNAME
    if not CHANNEL_USERNAME:
        await update.message.reply_text("❌ CHANNEL_USERNAME не задан в .env")
        return

    try:
        msg = await context.bot.send_message(chat_id=CHANNEL_USERNAME, text=post_text)
        await update.message.reply_text(f"✅ Опубликовано (msg_id={msg.message_id})")
    except Exception as e:
        logger.exception("handle_post_cmd error")
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def handle_schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/schedule YYYY-MM-DD HH:MM TEXT — schedule a post (MSK time)."""
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    if user_id not in OWNER_IDS:
        await update.message.reply_text("⛔ Команда доступна только администратору.")
        return

    # Parse: /schedule 2026-03-05 09:00 Текст поста
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_text("Использование: /schedule YYYY-MM-DD HH:MM <текст>")
        return

    date_str = args[0]
    time_str = args[1]
    post_text = " ".join(args[2:])

    try:
        from zoneinfo import ZoneInfo
        dt_msk = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        dt_msk = dt_msk.replace(tzinfo=ZoneInfo("Europe/Moscow"))
        dt_utc = dt_msk.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    except ValueError:
        await update.message.reply_text("❌ Неверный формат даты/времени. Пример: 2026-03-05 09:00")
        return

    from ..scheduled_posts import add_scheduled_post
    post_id = add_scheduled_post(post_text, dt_utc)
    await update.message.reply_text(
        f"✅ Пост #{post_id} запланирован на {date_str} {time_str} MSK"
    )


async def handle_posts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/posts — list pending scheduled posts."""
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    if user_id not in OWNER_IDS:
        await update.message.reply_text("⛔ Команда доступна только администратору.")
        return

    from ..scheduled_posts import list_scheduled_posts
    from zoneinfo import ZoneInfo
    posts = list_scheduled_posts(only_pending=True)
    if not posts:
        await update.message.reply_text("📭 Нет запланированных постов.")
        return

    lines = [f"📋 Запланировано постов: {len(posts)}\n"]
    for p in posts[:30]:  # limit display
        pub_at = p["publish_at"]
        if pub_at.tzinfo is None:
            pub_at = pub_at.replace(tzinfo=ZoneInfo("UTC"))
        pub_msk = pub_at.astimezone(ZoneInfo("Europe/Moscow"))
        preview = p["text"][:50].replace("\n", " ")
        pin_mark = " 📌" if p.get("pinned") else ""
        lines.append(f"#{p['id']} | {pub_msk:%d.%m %H:%M} | {preview}...{pin_mark}")

    await update.message.reply_text("\n".join(lines))


async def handle_delpost_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/delpost ID — delete a pending scheduled post."""
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    if user_id not in OWNER_IDS:
        await update.message.reply_text("⛔ Команда доступна только администратору.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /delpost <ID>")
        return

    post_id = int(context.args[0])
    from ..scheduled_posts import delete_scheduled_post
    if delete_scheduled_post(post_id):
        await update.message.reply_text(f"✅ Пост #{post_id} удалён.")
    else:
        await update.message.reply_text(f"❌ Пост #{post_id} не найден или уже опубликован.")


async def handle_loadposts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/loadposts — bulk load 20 predefined posts (19 scheduled + 1 pinned now)."""
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    if user_id not in OWNER_IDS:
        await update.message.reply_text("⛔ Команда доступна только администратору.")
        return

    from zoneinfo import ZoneInfo
    from ..scheduled_posts import (
        load_predefined_posts, publish_pinned_post_now,
        clear_unpublished_posts, CHANNEL_USERNAME,
    )

    if not CHANNEL_USERNAME:
        await update.message.reply_text("❌ CHANNEL_USERNAME не задан в .env")
        return

    # Clear old unpublished posts first
    deleted = clear_unpublished_posts()
    if deleted:
        await update.message.reply_text(f"🗑 Удалено {deleted} старых неопубликованных постов.")

    # Tomorrow in MSK
    from datetime import timedelta
    tomorrow_msk = datetime.now(ZoneInfo("Europe/Moscow")).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)

    # 1) Schedule 19 content posts
    count = load_predefined_posts(tomorrow_msk)

    # 2) Publish and pin the pinned post immediately
    await update.message.reply_text(f"📝 Загружено {count} постов в расписание.\n📌 Публикую закреп-пост...")
    pin_msg_id = await publish_pinned_post_now(context.bot)

    if pin_msg_id:
        await update.message.reply_text(
            f"✅ Готово!\n"
            f"📌 Закреп опубликован (msg_id={pin_msg_id})\n"
            f"📋 {count} постов запланировано начиная с {tomorrow_msk:%d.%m.%Y}\n"
            f"🕐 Расписание: 09:00, 15:00/16:00, 21:00 MSK"
        )
    else:
        await update.message.reply_text(
            f"⚠️ Закреп-пост не удалось опубликовать (проверь CHANNEL_USERNAME и права бота).\n"
            f"📋 {count} постов всё равно загружены в расписание."
        )


# ---------------------------------------------------------------------------
# /stats — Track Record (доступна ВСЕМ)
# ---------------------------------------------------------------------------
async def handle_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /stats — статистика точности Охотника."""
    if not update.message:
        return
    try:
        from ..track_record import get_stats, format_stats_message
        stats = get_stats(days=30)
        text = format_stats_message(stats)
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎯 Охотник", callback_data="MENU:HUNTER")],
                [InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")],
            ]),
        )
    except Exception:
        logger.exception("handle_stats error")
        await update.message.reply_text("Ошибка загрузки статистики. Попробуй позже.")


# ---------------------------------------------------------------------------
# /promo — activate promo code for free PRO
# ---------------------------------------------------------------------------
async def handle_promo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    if not user_id:
        return

    args = context.args
    if not args:
        await update.message.reply_text("Введи промокод: /promo КОД")
        return

    code = args[0].strip().upper()

    try:
        from sqlalchemy import text as sa_text
        from ..db import SessionLocal

        session = SessionLocal()
        try:
            # 1. Check if promo code exists
            row = session.exec(
                sa_text("SELECT code, max_uses, current_uses, days, expires_at FROM promo_codes WHERE code = :c"),
                params={"c": code},
            ).first()
            if not row:
                await update.message.reply_text("❌ Промокод не найден")
                return

            from ..pro_db import _row_to_dict
            promo = _row_to_dict(row)
            max_uses = promo.get("max_uses", 0)
            current_uses = promo.get("current_uses", 0)
            days = promo.get("days", 7)
            expires_at = promo.get("expires_at")

            # 2. Check expiry
            if expires_at is not None:
                from datetime import timezone as tz
                exp = expires_at
                if isinstance(exp, str):
                    exp = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                if hasattr(exp, 'tzinfo') and exp.tzinfo is None:
                    exp = exp.replace(tzinfo=tz.utc)
                if exp < datetime.now(tz.utc):
                    await update.message.reply_text("❌ Промокод истёк")
                    return

            # 3. Check limit
            if current_uses >= max_uses:
                await update.message.reply_text("❌ Промокод исчерпан (все активации использованы)")
                return

            # 4. Check if user already activated this code
            existing = session.exec(
                sa_text("SELECT id FROM promo_activations WHERE user_id = :uid AND code = :c"),
                params={"uid": user_id, "c": code},
            ).first()
            if existing:
                await update.message.reply_text("ℹ️ Ты уже активировал этот промокод")
                return

            # 5. Activate: grant PRO + record activation + increment counter
            from ..pro_db import grant_pro
            grant_pro(user_id, days=days)

            session.exec(
                sa_text("INSERT INTO promo_activations (user_id, code) VALUES (:uid, :c)"),
                params={"uid": user_id, "c": code},
            )
            session.exec(
                sa_text("UPDATE promo_codes SET current_uses = current_uses + 1 WHERE code = :c"),
                params={"c": code},
            )
            session.commit()

            new_uses = current_uses + 1
            remaining = max_uses - new_uses

            await update.message.reply_text(
                f"🎉 Промокод активирован!\n\n"
                f"✅ PRO на {days} дней — бесплатно\n\n"
                f"Тебе доступны:\n"
                f"🎯 Охотник — Топ-3 матча в 11:00\n"
                f"🔴 LIVE PRO — аналитика в реальном времени\n"
                f"🔴 LIVE — все идущие матчи\n\n"
                f"📝 Буду рад обратной связи! Пиши /feedback"
            )

            # Alert admin
            username = getattr(update.effective_user, "username", "") or ""
            first_name = getattr(update.effective_user, "first_name", "") or ""
            try:
                admin_id = OWNER_IDS.__iter__().__next__() if OWNER_IDS else None
                if admin_id and admin_id != user_id:
                    name = f"@{username}" if username else first_name or str(user_id)
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=(
                            f"🎁 Промокод: {name} активировал {code}\n"
                            f"PRO на {days} дней\n"
                            f"Осталось: {remaining}/{max_uses}"
                        ),
                    )
            except Exception:
                logger.warning("promo: failed to notify admin")

        finally:
            session.close()

    except Exception:
        logger.exception("handle_promo failed")
        await update.message.reply_text("❌ Ошибка при активации промокода. Попробуй позже.")


# ---------------------------------------------------------------------------
# /feedback — multi-step survey with inline buttons
# ---------------------------------------------------------------------------
# State: {user_id: {"q1": "...", "q2": "...", "q3": "...", "step": "text", "_ts": time()}}
_feedback_state: Dict[int, Dict[str, str]] = {}
_FEEDBACK_MAX_USERS = 50  # max concurrent feedback sessions
_FEEDBACK_TTL_S = 600  # 10 min — stale sessions auto-cleaned

_FB_Q1_TEXT = "📋 Шаг 1/4 — Насколько понятно как пользоваться ботом?"
_FB_Q1_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("😍 Всё понятно", callback_data="FB:Q1:1")],
    [InlineKeyboardButton("😐 Более-менее", callback_data="FB:Q1:2")],
    [InlineKeyboardButton("😕 Запутался", callback_data="FB:Q1:3")],
])
_FB_Q1_LABELS = {"1": "😍 Всё понятно", "2": "😐 Более-менее", "3": "😕 Запутался"}

_FB_Q2_TEXT = "📋 Шаг 2/4 — Охотник (Топ-3 матча) — полезен?"
_FB_Q2_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔥 Супер, буду следить", callback_data="FB:Q2:1")],
    [InlineKeyboardButton("👍 Нормально", callback_data="FB:Q2:2")],
    [InlineKeyboardButton("👎 Не интересно", callback_data="FB:Q2:3")],
])
_FB_Q2_LABELS = {"1": "🔥 Супер, буду следить", "2": "👍 Нормально", "3": "👎 Не интересно"}

_FB_Q3_TEXT = "📋 Шаг 3/4 — Готов платить 299₽/неделю за PRO?"
_FB_Q3_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("💰 Да, стоит того", callback_data="FB:Q3:1")],
    [InlineKeyboardButton("🤔 Дороговато", callback_data="FB:Q3:2")],
    [InlineKeyboardButton("❌ Нет", callback_data="FB:Q3:3")],
])
_FB_Q3_LABELS = {"1": "💰 Да, стоит того", "2": "🤔 Дороговато", "3": "❌ Нет"}

_FB_TEXT_PROMPT = (
    "📋 Шаг 4/4 — Что добавить или улучшить?\n\n"
    "Напиши свободным текстом (или /skip чтобы пропустить)"
)

# Set of user_ids waiting for free-text feedback input
_feedback_waiting: set = set()


async def handle_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    if not user_id:
        return

    # Check if user already submitted feedback
    try:
        from sqlalchemy import text as sa_text
        from ..db import SessionLocal
        session = SessionLocal()
        try:
            existing = session.exec(
                sa_text("SELECT id FROM feedback WHERE user_id = :uid"),
                params={"uid": user_id},
            ).first()
        finally:
            session.close()
        if existing:
            await update.message.reply_text("ℹ️ Ты уже оставлял отзыв. Спасибо!")
            return
    except Exception:
        pass  # DB check failed — allow anyway

    # Cleanup stale feedback sessions before starting new one
    import time as _time
    now_ts = _time.time()
    stale = [uid for uid, st in _feedback_state.items()
             if now_ts - st.get("_ts", 0) > _FEEDBACK_TTL_S]
    for uid in stale:
        _feedback_state.pop(uid, None)
        _feedback_waiting.discard(uid)

    # Start survey: step 1
    _feedback_state[user_id] = {"_ts": now_ts}
    await update.message.reply_text(_FB_Q1_TEXT, reply_markup=_FB_Q1_KB)


async def _handle_feedback_callback(q, user_id: int, data: str, context) -> bool:
    """Handle FB:* callbacks. Returns True if handled."""
    if not data.startswith("FB:"):
        return False

    parts = data.split(":")
    if len(parts) != 3:
        return False

    question, answer = parts[1], parts[2]
    state = _feedback_state.get(user_id)

    if state is None:
        # No active survey — maybe expired
        await _safe_edit_or_send(q, "ℹ️ Опрос не активен. Начни заново: /feedback")
        return True

    if question == "Q1":
        state["q1"] = answer
        await _safe_edit_or_send(
            q,
            f"{_FB_Q1_TEXT}\n✅ {_FB_Q1_LABELS.get(answer, answer)}\n\n{_FB_Q2_TEXT}",
            reply_markup=_FB_Q2_KB,
        )
        return True

    if question == "Q2":
        state["q2"] = answer
        await _safe_edit_or_send(
            q,
            f"{_FB_Q2_TEXT}\n✅ {_FB_Q2_LABELS.get(answer, answer)}\n\n{_FB_Q3_TEXT}",
            reply_markup=_FB_Q3_KB,
        )
        return True

    if question == "Q3":
        state["q3"] = answer
        # Move to text input step
        _feedback_waiting.add(user_id)
        await _safe_edit_or_send(
            q,
            f"{_FB_Q3_TEXT}\n✅ {_FB_Q3_LABELS.get(answer, answer)}",
        )
        # Send text prompt as a new message (can't edit into text input)
        try:
            await context.bot.send_message(chat_id=user_id, text=_FB_TEXT_PROMPT)
        except Exception:
            logger.warning("feedback: failed to send text prompt to %s", user_id)
        return True

    return False


async def _finish_feedback(update: Update, context, user_id: int, free_text: str) -> None:
    """Save feedback to DB, forward to admin, grant +3 days PRO."""
    _feedback_waiting.discard(user_id)
    state = _feedback_state.pop(user_id, {})

    q1 = state.get("q1", "")
    q2 = state.get("q2", "")
    q3 = state.get("q3", "")

    # Save to DB
    try:
        from sqlalchemy import text as sa_text
        from ..db import SessionLocal
        session = SessionLocal()
        try:
            session.exec(
                sa_text("""
                    INSERT INTO feedback (user_id, q1, q2, q3, free_text)
                    VALUES (:uid, :q1, :q2, :q3, :ft)
                """),
                params={"uid": user_id, "q1": q1, "q2": q2, "q3": q3, "ft": free_text[:2000]},
            )
            session.commit()
        finally:
            session.close()
    except Exception:
        logger.exception("feedback: failed to save to DB")

    # Grant +3 days PRO
    try:
        from ..pro_db import grant_pro
        grant_pro(user_id, days=3)
    except Exception:
        logger.exception("feedback: failed to grant PRO reward")

    # Thank user
    await update.message.reply_text(
        "🙏 Спасибо за отзыв! Ты помогаешь сделать Betly лучше.\n\n"
        "🎁 +3 дня PRO за обратную связь!"
    )

    # Forward to admin
    username = getattr(update.effective_user, "username", "") or ""
    first_name = getattr(update.effective_user, "first_name", "") or ""
    name = f"@{username}" if username else first_name or str(user_id)

    admin_id = next(iter(OWNER_IDS), None)
    if admin_id:
        try:
            admin_text = (
                f"📋 ФИДБЭК от {name}:\n"
                f"1. Понятность: {_FB_Q1_LABELS.get(q1, q1 or '—')}\n"
                f"2. Охотник: {_FB_Q2_LABELS.get(q2, q2 or '—')}\n"
                f"3. Оплата: {_FB_Q3_LABELS.get(q3, q3 or '—')}\n"
                f"4. Текст: {free_text[:500] if free_text else '—'}"
            )
            await context.bot.send_message(chat_id=admin_id, text=admin_text)
        except Exception:
            logger.warning("feedback: failed to forward to admin")


async def handle_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/skip — skips the free-text step of feedback survey."""
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    if user_id in _feedback_waiting:
        await _finish_feedback(update, context, user_id, "")
    else:
        await update.message.reply_text("Нечего пропускать.")


# ---------------------------------------------------------------------------
# 🔴 LIVE — all in-progress matches across all sports (PRO only)
# ---------------------------------------------------------------------------
_LIVE_SPORT_EMOJI = {
    "ice-hockey": "🏒",
    "football": "⚽",
    "basketball": "🏀",
    "tennis": "🎾",
    "mma": "🥊",
}

_LIVE_SPORT_TITLE = {
    "ice-hockey": "Хоккей",
    "football": "Футбол",
    "basketball": "Баскетбол",
    "tennis": "Теннис",
    "mma": "MMA",
}


async def _handle_live_button(update: Update, user_id: int) -> None:
    """Show all LIVE matches across all sports. PRO-only feature."""

    # --- PRO paywall ---
    user_is_pro = False
    try:
        user_is_pro = is_pro(user_id)
    except Exception:
        pass
    in_trial = _is_in_hunter_trial(user_id)

    if not user_is_pro and not in_trial:
        await update.message.reply_text(
            "🔴 LIVE — только для PRO-подписчиков\n\n"
            "Смотри идущие матчи в реальном времени\n"
            "с AI-аналитикой, статистикой и коэффициентами.\n\n"
            "🌟 Оформи PRO — от 299₽/неделю",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌟 Оформить PRO", callback_data="MENU:PREMIUM")],
                [InlineKeyboardButton("🎁 Попробовать 3 дня бесплатно", callback_data="PRO:trial")],
            ]),
        )
        return

    # --- Fetch live matches ---
    try:
        by_sport, upcoming = await _fetch_all_live_matches()
    except Exception:
        logger.exception("_fetch_all_live_matches failed")
        await update.message.reply_text(
            "⚠️ Не удалось загрузить LIVE-матчи. Попробуй позже.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    # --- No live matches → show upcoming ---
    if not by_sport:
        lines = ["🔴 LIVE матчи\n\nСейчас нет идущих матчей.\n"]
        if upcoming:
            lines.append("⏰ Ближайшие матчи:")
            for m in upcoming:
                emoji = _LIVE_SPORT_EMOJI.get(m.sport_slug, "🏆")
                start = _extract_hhmm(m.start_time or "")  # HH:MM
                title = (m.title or "Матч")[:40]
                league = (m.league or "")[:20]
                lines.append(f"  {emoji} {start} {title} ({league})")
        lines.append("\n💡 Матчи обычно начинаются с 12:00 MSK")
        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Обновить", callback_data="LIVE:REFRESH")],
                [InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")],
            ]),
        )
        return

    # --- Build LIVE matches message ---
    text = "🔴 LIVE матчи сейчас\n━━━━━━━━━━━━━━━━━━\n"
    buttons: List[List[InlineKeyboardButton]] = []

    for sport in _LIVE_SPORTS:
        matches = by_sport.get(sport)
        if not matches:
            continue
        emoji = _LIVE_SPORT_EMOJI.get(sport, "🏆")
        sport_title = _LIVE_SPORT_TITLE.get(sport, sport)
        text += f"\n{emoji} {sport_title}:\n"

        for m in matches[:5]:
            title = (m.title or "Матч")[:35]
            score = _normalize_score(m.score) or "?:?"
            league = (m.league or "")[:15]
            status = (m.status or "").strip()

            text += f"  🔴 {title} {score}"
            if league:
                text += f" ({league})"
            text += "\n"

            # Inline button
            btn_label = f"🔴 {title} {score}"
            if len(btn_label) > 55:
                btn_label = btn_label[:54] + "…"
            buttons.append([InlineKeyboardButton(
                btn_label,
                callback_data=f"MATCH:{sport}:{m.id}",
            )])

    text += "\nНажми на матч для анализа 👇"

    buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data="LIVE:REFRESH")])
    buttons.append([InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")])

    await update.message.reply_text(
        _truncate_tg(text),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if _is_duplicate(update.update_id):
        return

    user_id = update.effective_user.id if update.effective_user else 0
    text_raw = (update.message.text or "").strip()
    norm = text_raw.lower().strip()

    logger.info("tg.handle_message user_id=%s text=%r", user_id, text_raw)

    # --- Feedback text step (step 4 of survey) ---
    if user_id in _feedback_waiting and text_raw:
        free_text = "" if norm == "/skip" else text_raw
        await _finish_feedback(update, context, user_id, free_text)
        return

    # --- Reply keyboard: 🔴 LIVE button ---
    if text_raw.strip() == "🔴 LIVE":
        await _handle_live_button(update, user_id)
        return

    # --- Reply keyboard: sport buttons ---
    sport_slug = _REPLY_SPORT_MAP.get(text_raw.strip())
    if sport_slug:
        if not _is_allowed_sport(user_id, sport_slug):
            title = SPORT_LABELS.get(sport_slug, sport_slug)
            await update.message.reply_text(
                f"🔒 {title} недоступен по твоему тарифу.",
                reply_markup=MAIN_MENU_KEYBOARD,
            )
            return
        # F1 has custom navigation (races, not daily fixtures)
        if sport_slug == "formula1":
            try:
                text, kb = await _render_f1_home(user_id)
                await update.message.reply_text(text, reply_markup=kb)
            except Exception:
                logger.exception("F1 render failed")
                await update.message.reply_text("🏎 Formula 1 временно недоступна. Попробуй позже.")
            return
        text, kb = await _render_sport_nav_root(user_id, sport_slug)
        txt = _truncate_tg(text)
        await update.message.reply_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    # --- Reply keyboard: PRO button ---
    if text_raw.strip() == "🌟 PRO":
        txt = _truncate_tg(_text_buy_pro(user_id))
        await update.message.reply_text(
            _safe_markdown(txt),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_buy_pro(),
        )
        return

    # быстрый вход в матчи
    if "матчи сегодня" in norm:
        await update.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports(user_id))
        return

    # premium / pro screen (когда кнопка прилетает как текст)
    if (
        "premium" in norm
        or "премиум" in norm
        or "pro" == norm
        or "оформить pro" in norm
        or "купить pro" in norm
        or text_raw.strip() in {"⭐ Premium", "⭐ Премиум"}
    ):
        txt = _truncate_tg(_text_buy_pro(user_id))
        await update.message.reply_text(
            _safe_markdown(txt),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_buy_pro(),
        )
        return

    # остальное — в агента
    reply = await call_agent_local(user_id, text_raw)
    txt = _truncate_tg(reply)
    await update.message.reply_text(
        _safe_markdown(txt),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_main_menu(),
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query:
        return
    if _is_duplicate(update.update_id):
        return

    q = update.callback_query
    data = (q.data or "").strip()
    user_id = update.effective_user.id if update.effective_user else 0
    chat_id = update.effective_chat.id if update.effective_chat else None

    logger.info("tg.callback user_id=%s data=%r", user_id, data)

    try:
        await q.answer()
    except Exception:
        pass

    try:
        if chat_id is not None:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    if data in {"NOOP", ""}:
        return

    # FB:* — feedback survey callbacks
    if data.startswith("FB:"):
        try:
            handled = await _handle_feedback_callback(q, user_id, data, context)
            if handled:
                return
        except Exception:
            logger.exception("feedback callback failed for data=%r", data)
        return

    # F1:* — Formula 1 navigation callbacks
    if data.startswith("F1:"):
        try:
            if data == "F1:HOME":
                txt, kb = await _render_f1_home(user_id)
            elif data == "F1:DRIVERS":
                txt, kb = await _render_f1_drivers(user_id)
            elif data == "F1:TEAMS":
                txt, kb = await _render_f1_teams(user_id)
            elif data == "F1:CALENDAR":
                txt, kb = await _render_f1_calendar(user_id)
            else:
                return
            await _safe_edit_or_send(q, txt, reply_markup=kb)
        except Exception:
            logger.exception("F1 callback failed for data=%r", data)
        return

    # BACK:MENU
    if data == "BACK:MENU":
        await _safe_edit_or_send(q, MAIN_MENU_TEXT, reply_markup=kb_main_menu())
        return

    # BUY:PRO — show tariff selection
    if data == "BUY:PRO":
        txt = _truncate_tg(_text_buy_pro(user_id))
        await _safe_edit_or_send(q, _safe_markdown(txt), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_buy_pro())
        return

    # PRO:<tariff_key> — send invoice for payment (ЮKassa or Stars)
    if data.startswith("PRO:") and data != "PRO:TRIAL":
        tariff_key = data.split(":", 1)[1].strip().lower()
        try:
            from .payments import send_invoice
            await q.answer()
            await send_invoice(update, context, tariff_key)
        except Exception:
            logger.exception("send_invoice failed for tariff=%s", tariff_key)
            await q.answer("Ошибка при создании счёта. Попробуй позже.", show_alert=True)
        return

    # PRO:TRIAL — активация бесплатного пробного периода
    if data == "PRO:TRIAL":
        trial_ok = False
        try:
            from ..user_store import get_user_by_tg_id
            from datetime import timedelta, timezone as tz
            u = get_user_by_tg_id(user_id)
            already_tried = u and getattr(u, 'trial_started_at', None)
            if already_tried:
                txt = "ℹ️ Пробный период уже был использован.\n\nОформи PRO, чтобы продолжить!"
            else:
                from ..pro_db import grant_pro
                grant_pro(user_id, days=3)
                # Mark trial start
                try:
                    from sqlalchemy import text as sa_text
                    from ..db import SessionLocal
                    session = SessionLocal()
                    try:
                        session.exec(
                            sa_text("UPDATE users SET trial_started_at = NOW() WHERE tg_user_id = :uid"),
                            params={"uid": user_id},
                        )
                        session.commit()
                    finally:
                        session.close()
                except Exception:
                    logger.exception("Failed to set trial_started_at")
                txt = "🎁 Пробный период активирован!\n\n3 дня полного PRO-доступа.\n\nПопробуй Охотника — топ матчи дня уже ждут!"
                trial_ok = True
        except Exception:
            logger.exception("PRO:TRIAL failed")
            txt = "⚠️ Не удалось активировать пробный период. Попробуй позже."

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯 Охотник", callback_data="MENU:HUNTER")] if trial_ok else [],
            [InlineKeyboardButton("⬅️ Назад к тарифам", callback_data="MENU:PREMIUM")],
            [InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")],
        ])
        # Filter empty rows
        kb = InlineKeyboardMarkup([r for r in kb.inline_keyboard if r])
        try:
            await q.edit_message_text(txt, reply_markup=kb)
        except Exception:
            await q.message.reply_text(txt, reply_markup=kb)
        return

    # PAY:<tariff_key> — send invoice for selected tariff
    if data.startswith("PAY:"):
        tariff_key = data.split(":", 1)[1].strip().lower()
        try:
            from .payments import send_invoice
            await q.answer()
            await send_invoice(update, context, tariff_key)
        except Exception:
            logger.exception("send_invoice failed for tariff=%s", tariff_key)
            await q.answer("Ошибка при создании счёта. Попробуй позже.", show_alert=True)
        return

    # ONBOARD:START
    if data == "ONBOARD:START":
        try:
            set_user_seen_intro(
                user_id,
                username=getattr(update.effective_user, 'username', None),
                first_name=getattr(update.effective_user, 'first_name', None),
                last_name=getattr(update.effective_user, 'last_name', None),
            )
        except Exception:
            logger.exception("set_user_seen_intro failed")
        # Remove inline buttons from onboarding message
        try:
            await q.edit_message_text(MAIN_MENU_TEXT)
        except Exception:
            pass
        # Send new message with persistent reply keyboard
        await q.message.reply_text(
            "Выбери спорт 👇",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    # ONBOARD:HELP
    if data == "ONBOARD:HELP":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Начать", callback_data="ONBOARD:START")],
        ])
        try:
            await q.edit_message_text(ONBOARDING_HOW_IT_WORKS, reply_markup=kb)
        except Exception:
            await q.message.reply_text(ONBOARDING_HOW_IT_WORKS, reply_markup=kb)
        return

    # MENU:HUNTER
    if data == "MENU:HUNTER":
        await _handle_hunter(q, user_id)
        return

    # HUNTER:EXAMPLE
    if data == "HUNTER:EXAMPLE":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌟 Оформить PRO", callback_data="MENU:PREMIUM")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="MENU:HUNTER")],
        ])
        try:
            await q.edit_message_text(HUNTER_EXAMPLE_TEXT, reply_markup=kb)
        except Exception:
            await q.message.reply_text(HUNTER_EXAMPLE_TEXT, reply_markup=kb)
        return

    # HUNTER:REFRESH — admin-only inline regeneration
    if data == "HUNTER:REFRESH":
        if user_id not in OWNER_IDS:
            return

        try:
            await q.edit_message_text("🔄 Перегенерирую Охотника... 1-2 минуты.")
        except Exception:
            pass

        try:
            from ..daily_pro import run_daily_hunter
            await run_daily_hunter(bot=context.bot)
        except Exception:
            logger.exception("HUNTER:REFRESH failed")
            try:
                await q.edit_message_text("❌ Ошибка при генерации. Смотри логи.")
            except Exception:
                pass
            return

        picks = _get_today_picks()
        top3 = [p for p in picks if p.get("pick_type") == "top3"]
        if not top3:
            try:
                await q.edit_message_text("⚠️ Пайплайн отработал, но пиков нет.")
            except Exception:
                pass
            return

        txt = "✅ Перегенерировано!\n\n" + _format_hunter_picks_text(picks)
        rows = []
        for p in top3[:3]:
            title = (p.get("title") or "Матч")[:30]
            mid = p.get("match_id", "")
            sport = p.get("sport_slug", "ice-hockey")
            rows.append([InlineKeyboardButton(
                f"🔍 {title}",
                callback_data=f"MATCH:{sport}:{mid}"
            )])
        rows.append([InlineKeyboardButton("🔄 Перегенерировать", callback_data="HUNTER:REFRESH")])
        rows.append([InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")])

        try:
            await q.edit_message_text(
                _truncate_tg(txt),
                reply_markup=InlineKeyboardMarkup(rows),
            )
        except Exception:
            await q.message.reply_text(
                _truncate_tg(txt),
                reply_markup=InlineKeyboardMarkup(rows),
            )
        return

    # HUNTER:DETAIL:<match_id>
    if data.startswith("HUNTER:DETAIL:"):
        match_id = data.split(":", 2)[2].strip()
        await _handle_hunter_detail(q, user_id, match_id)
        return

    # MENU:ABOUT
    if data == "MENU:ABOUT":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ В меню", callback_data="BACK:MENU")],
        ])
        try:
            await q.edit_message_text(ABOUT_TEXT, reply_markup=kb)
        except Exception:
            await q.message.reply_text(ABOUT_TEXT, reply_markup=kb)
        return

    # MENU:MATCHES / BACK:MATCHES_MENU => выбор спорта
    if data in {"MENU:MATCHES", "BACK:MATCHES_MENU"}:
        text = "🏟 Выбери спорт:"
        try:
            await q.edit_message_text(text, reply_markup=kb_sports(user_id))
        except Exception:
            await q.message.reply_text(text, reply_markup=kb_sports(user_id))
        return

    # BACK:MATCHES => вернуться на последний экран матчей
    if data == "BACK:MATCHES":
        text, kb = _nav_back_to_last(user_id)
        txt = _truncate_tg(text)
        try:
            await q.edit_message_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await q.message.reply_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    if data == "MENU:STATS":
        try:
            from ..track_record import get_stats, format_stats_message
            stats = get_stats(days=30)
            txt = _truncate_tg(format_stats_message(stats))
        except Exception:
            logger.exception("MENU:STATS error")
            txt = "Ошибка загрузки статистики."
        stats_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯 Охотник", callback_data="MENU:HUNTER")],
            [InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")],
        ])
        try:
            await q.edit_message_text(txt, reply_markup=stats_kb)
        except Exception:
            await q.message.reply_text(txt, reply_markup=stats_kb)
        return

    if data == "MENU:PROFILE":
        reply = await call_agent_local(user_id, "профиль")
        txt = _truncate_tg(reply)
        try:
            await q.edit_message_text(_safe_markdown(txt), reply_markup=kb_main_menu(), parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await q.message.reply_text(_safe_markdown(txt), reply_markup=kb_main_menu(), parse_mode=ParseMode.MARKDOWN)
        return

    if data == "MENU:PREMIUM":
        txt = _truncate_tg(_text_buy_pro(user_id))
        try:
            await q.edit_message_text(_safe_markdown(txt), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_buy_pro())
        except Exception:
            await q.message.reply_text(_safe_markdown(txt), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_buy_pro())
        return

    # SPORT_LOCKED
    if data.startswith("SPORT_LOCKED:"):
        slug = data.split(":", 1)[1].strip().lower()
        title = SPORT_LABELS.get(slug, slug)
        txt = (
            f"🔒 {title} недоступен по твоему тарифу.\n\n"
            "Сейчас доступно: " + ", ".join(SPORT_LABELS.get(s, s) for s in DEFAULT_SPORTS)
        )
        try:
            await q.edit_message_text(txt, reply_markup=kb_sports(user_id))
        except Exception:
            await q.message.reply_text(txt, reply_markup=kb_sports(user_id))
        return

    # SPORT
    if data.startswith("SPORT:"):
        sport_slug = data.split(":", 1)[1].strip().lower()
        if not _is_allowed_sport(user_id, sport_slug):
            title = SPORT_LABELS.get(sport_slug, sport_slug)
            txt = f"🔒 {title} недоступен по твоему тарифу."
            try:
                await q.edit_message_text(txt, reply_markup=kb_sports(user_id))
            except Exception:
                await q.message.reply_text(txt, reply_markup=kb_sports(user_id))
            return

        text, kb = await _render_sport_nav_root(user_id, sport_slug)
        txt = _truncate_tg(text)
        try:
            await q.edit_message_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await q.message.reply_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    # NAV:COUNTRY
    if data.startswith("NAV:COUNTRY:"):
        parts = data.split(":")
        if len(parts) < 4:
            return
        sport_slug = parts[2].strip().lower()
        ckey = parts[3].strip()

        st = _NAV_BY_USER.get(user_id)
        if st:
            st.last_screen = "LEAGUES"
            st.last_ckey = ckey
            st.last_lkey = ""
            st.last_page = 1

        text = _text_leagues(user_id, ckey)
        kb = _kb_leagues(user_id, sport_slug, ckey)
        txt = _truncate_tg(text)
        try:
            await q.edit_message_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await q.message.reply_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    # NAV:LEAGUE
    if data.startswith("NAV:LEAGUE:"):
        parts = data.split(":")
        if len(parts) < 5:
            return
        sport_slug = parts[2].strip().lower()
        ckey = parts[3].strip()
        lkey = parts[4].strip()

        st = _NAV_BY_USER.get(user_id)
        if st:
            st.last_screen = "MATCHES"
            st.last_ckey = ckey
            st.last_lkey = lkey
            st.last_page = 1

        text = _text_matches(user_id, ckey, lkey, page=1)
        kb = _kb_matches(user_id, sport_slug, ckey, lkey, page=1)
        txt = _truncate_tg(text)
        try:
            await q.edit_message_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await q.message.reply_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    # NAV:PAGE
    if data.startswith("NAV:PAGE:"):
        parts = data.split(":")
        if len(parts) < 6:
            return
        sport_slug = parts[2].strip().lower()
        ckey = parts[3].strip()
        lkey = parts[4].strip()
        try:
            page = int(parts[5])
        except Exception:
            page = 1

        st = _NAV_BY_USER.get(user_id)
        if st:
            st.last_screen = "MATCHES"
            st.last_ckey = ckey
            st.last_lkey = lkey
            st.last_page = page

        text = _text_matches(user_id, ckey, lkey, page=page)
        kb = _kb_matches(user_id, sport_slug, ckey, lkey, page=page)
        txt = _truncate_tg(text)
        try:
            await q.edit_message_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await q.message.reply_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    # BACK:COUNTRIES
    if data.startswith("BACK:COUNTRIES:"):
        parts = data.split(":")
        if len(parts) < 3:
            return
        sport_slug = parts[2].strip().lower()

        st = _NAV_BY_USER.get(user_id)
        if st:
            st.last_screen = "COUNTRIES"
            st.last_ckey = ""
            st.last_lkey = ""
            st.last_page = 1

        text = _text_countries(user_id, sport_slug)
        kb = _kb_countries(user_id, sport_slug)
        txt = _truncate_tg(text)
        try:
            await q.edit_message_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await q.message.reply_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    # BACK:LEAGUES
    if data.startswith("BACK:LEAGUES:"):
        parts = data.split(":")
        if len(parts) < 4:
            return
        sport_slug = parts[2].strip().lower()
        ckey = parts[3].strip()

        st = _NAV_BY_USER.get(user_id)
        if st:
            st.last_screen = "LEAGUES"
            st.last_ckey = ckey
            st.last_lkey = ""
            st.last_page = 1

        text = _text_leagues(user_id, ckey)
        kb = _kb_leagues(user_id, sport_slug, ckey)
        txt = _truncate_tg(text)
        try:
            await q.edit_message_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await q.message.reply_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    # LIVE:REFRESH — re-fetch and update LIVE matches inline
    if data == "LIVE:REFRESH":
        user_is_pro = False
        try:
            user_is_pro = is_pro(user_id)
        except Exception:
            pass
        in_trial = _is_in_hunter_trial(user_id)

        if not user_is_pro and not in_trial:
            try:
                await q.edit_message_text(
                    "🔴 LIVE — только для PRO-подписчиков\n\n"
                    "Смотри идущие матчи в реальном времени\n"
                    "с AI-аналитикой, статистикой и коэффициентами.\n\n"
                    "🌟 Оформи PRO — от 299₽/неделю",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🌟 Оформить PRO", callback_data="MENU:PREMIUM")],
                        [InlineKeyboardButton("🎁 Попробовать 3 дня бесплатно", callback_data="PRO:trial")],
                    ]),
                )
            except Exception:
                pass
            return

        # Force cache refresh by resetting it
        global _ALL_LIVE_CACHE
        _ALL_LIVE_CACHE = None

        try:
            by_sport, upcoming = await _fetch_all_live_matches()
        except Exception:
            logger.exception("LIVE:REFRESH failed")
            return

        if not by_sport:
            lines = ["🔴 LIVE матчи\n\nСейчас нет идущих матчей.\n"]
            if upcoming:
                lines.append("⏰ Ближайшие матчи:")
                for m in upcoming:
                    emoji = _LIVE_SPORT_EMOJI.get(m.sport_slug, "🏆")
                    start = _extract_hhmm(m.start_time or "")
                    title = (m.title or "Матч")[:40]
                    league = (m.league or "")[:20]
                    lines.append(f"  {emoji} {start} {title} ({league})")
            lines.append("\n💡 Матчи обычно начинаются с 12:00 MSK")
            try:
                await q.edit_message_text(
                    "\n".join(lines),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔄 Обновить", callback_data="LIVE:REFRESH")],
                        [InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")],
                    ]),
                )
            except Exception:
                pass
            return

        text = "🔴 LIVE матчи сейчас\n━━━━━━━━━━━━━━━━━━\n"
        buttons: List[List[InlineKeyboardButton]] = []

        for sport in _LIVE_SPORTS:
            matches = by_sport.get(sport)
            if not matches:
                continue
            emoji = _LIVE_SPORT_EMOJI.get(sport, "🏆")
            sport_title = _LIVE_SPORT_TITLE.get(sport, sport)
            text += f"\n{emoji} {sport_title}:\n"

            for m in matches[:5]:
                title = (m.title or "Матч")[:35]
                score = _normalize_score(m.score) or "?:?"
                league = (m.league or "")[:15]
                text += f"  🔴 {title} {score}"
                if league:
                    text += f" ({league})"
                text += "\n"

                btn_label = f"🔴 {title} {score}"
                if len(btn_label) > 55:
                    btn_label = btn_label[:54] + "…"
                buttons.append([InlineKeyboardButton(
                    btn_label,
                    callback_data=f"MATCH:{sport}:{m.id}",
                )])

        text += "\nНажми на матч для анализа 👇"
        buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data="LIVE:REFRESH")])
        buttons.append([InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")])

        try:
            await q.edit_message_text(
                _truncate_tg(text),
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        except Exception:
            pass
        return

    # MATCH open
    if data.startswith("MATCH:"):
        parts = data.split(":")
        if len(parts) >= 3:
            sport_slug = parts[1].strip().lower()
            match_id = ":".join(parts[2:]).strip()
        else:
            sport_slug = "ice-hockey"
            match_id = data.split(":", 1)[1].strip()

        # прогреваем контекст для parsing.py (чтобы match_details работал даже без кеша)
        try:
            await call_agent_local(user_id, f"матчи сегодня {sport_slug}")
        except Exception:
            logger.exception("pre-cache matches failed")

        reply = await call_agent_local(user_id, f"матч {match_id}")

        # Append odds table + line movement for PRO users
        odds_section = ""
        try:
            if is_pro(user_id):
                odds_section = await _get_odds_section(sport_slug, match_id, reply)
        except Exception:
            logger.debug("Odds section failed for MATCH:%s", match_id)

        txt = _truncate_tg(reply + odds_section)

        await _safe_edit_or_send(
            q, _safe_markdown(txt),
            reply_markup=kb_match_hub(match_id),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # UI actions
    if data.startswith("UI:"):
        parts = data.split(":")
        if len(parts) < 4:
            await _safe_edit_or_send(q, "⚠️ Некорректная команда.", reply_markup=kb_main_menu())
            return

        match_id = parts[1].strip()
        mode = parts[2].strip().lower()
        action = parts[3].strip().lower()

        reply = await call_agent_local(user_id, f"ui match {match_id} {mode} {action}")
        txt = _truncate_tg(reply)

        await _safe_edit_or_send(
            q, _safe_markdown(txt),
            reply_markup=kb_match_hub(match_id),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # fallback
    await _safe_edit_or_send(q, "Не понял действие. Открой меню.", reply_markup=kb_main_menu())


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Telegram handler error", exc_info=context.error)

    # Alert admin about the error
    if context.error:
        try:
            from ..alerting import send_alert
            uid = None
            uname = None
            if isinstance(update, Update) and update.effective_user:
                uid = update.effective_user.id
                uname = getattr(update.effective_user, "username", None)
            await send_alert(
                "ERROR", "telegram_handler", context.error,
                user_id=uid, username=uname,
            )
        except Exception:
            pass

    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Временно недоступно, попробуй позже.",
            )
    except Exception:
        logger.exception("Failed to send error message to user")


# ============================================================
# Telegram init / webhook
# ============================================================
def create_application() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("pro", handle_pro))
    app.add_handler(CommandHandler("hunter", handle_hunter))
    app.add_handler(CommandHandler("hunter_refresh", handle_hunter_refresh))
    app.add_handler(CommandHandler("hunter_status", handle_hunter_status))
    # Scheduled channel posts (admin only)
    app.add_handler(CommandHandler("post", handle_post_cmd))
    app.add_handler(CommandHandler("schedule", handle_schedule_cmd))
    app.add_handler(CommandHandler("posts", handle_posts_cmd))
    app.add_handler(CommandHandler("delpost", handle_delpost_cmd))
    app.add_handler(CommandHandler("loadposts", handle_loadposts_cmd))
    app.add_handler(CommandHandler("promo", handle_promo))
    app.add_handler(CommandHandler("feedback", handle_feedback))
    app.add_handler(CommandHandler("stats", handle_stats))
    app.add_handler(CommandHandler("skip", handle_skip))

    # Payments: pre-checkout + successful payment
    try:
        from .payments import handle_pre_checkout as _pre_checkout
        from .payments import handle_successful_payment as _success_pay
        app.add_handler(PreCheckoutQueryHandler(_pre_checkout))
        app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, _success_pay))
    except Exception:
        logger.exception("Failed to wire payment handlers")

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(handle_error)
    return app


async def telegram_startup() -> None:
    global _telegram_app
    if _telegram_app is not None:
        return

    _telegram_app = create_application()

    await _telegram_app.initialize()
    await _telegram_app.start()

    # Init alerting system
    try:
        from ..alerting import init_alerting
        init_alerting(_telegram_app.bot)
    except Exception:
        logger.exception("Failed to init alerting")

    webhook_url = WEBHOOK_URL
    if not webhook_url:
        if not PUBLIC_URL:
            logger.warning("PUBLIC_URL is missing; webhook not set")
            return
        webhook_url = PUBLIC_URL.rstrip("/") + WEBHOOK_PATH

    try:
        ok = await _telegram_app.bot.set_webhook(webhook_url)
        logger.info("Telegram webhook set: %s (ok=%s)", webhook_url, ok)
    except Exception:
        logger.exception("Failed to set webhook")

    # Register bot commands (visible in Telegram menu button)
    try:
        await _telegram_app.bot.set_my_commands([
            BotCommand("start", "Главное меню"),
            BotCommand("hunter", "🎯 Охотник — топ матчи дня"),
            BotCommand("pro", "🌟 PRO подписка"),
        ])
    except Exception:
        logger.exception("Failed to set bot commands")


async def telegram_shutdown() -> None:
    global _telegram_app
    if _telegram_app is None:
        return
    try:
        await _telegram_app.stop()
        await _telegram_app.shutdown()
    finally:
        _telegram_app = None


# ============================================================
# FastAPI routes mount for Telegram webhook (Render / FastAPI)
# ============================================================
telegram_router = APIRouter()

# Deduplication: Telegram may retry webhooks — skip already-processed update_ids
_seen_update_ids: set = set()
_SEEN_MAX = 300  # keep last N to avoid memory growth


@telegram_router.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    payload = await request.json()

    if _telegram_app is None:
        logger.error("Telegram webhook received, but PTB app is not initialized (_telegram_app is None)")
        return JSONResponse({"ok": False, "error": "ptb_not_initialized"}, status_code=503)

    # Deduplicate: skip if we already processed this update_id
    update_id = payload.get("update_id")
    if update_id is not None:
        if update_id in _seen_update_ids:
            logger.debug("Skipping duplicate update_id=%s", update_id)
            return JSONResponse({"ok": True})
        _seen_update_ids.add(update_id)
        # Trim set to avoid unbounded growth
        if len(_seen_update_ids) > _SEEN_MAX:
            # Remove oldest entries (approx — sets are unordered, but update_ids are monotonic)
            to_remove = sorted(_seen_update_ids)[:_SEEN_MAX // 2]
            _seen_update_ids.difference_update(to_remove)

    try:
        upd = Update.de_json(payload, _telegram_app.bot)
        await _telegram_app.process_update(upd)
        return JSONResponse({"ok": True})
    except Exception:
        logger.exception("Failed to process telegram update")
        return JSONResponse({"ok": False}, status_code=500)


def mount_telegram_routes(app: FastAPI) -> None:
    app.include_router(telegram_router)
    logger.info("Telegram routes mounted.")
