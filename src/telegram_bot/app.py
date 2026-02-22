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
        [KeyboardButton("🥊 MMA"), KeyboardButton("🔴 LIVE")],
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
                # Collect not-started for "upcoming" fallback
                s_lower = status.lower()
                if s_lower in {"notstarted", "ns", "not started", "scheduled", ""}:
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


def _compact_match_btn_title(title: str, score: str, status: str) -> str:
    t = (title or "").strip() or "Матч"
    sc = (score or "").strip()
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
            "📅 Сезон — 4 990 ₽ (экономия 40%)\n\n"
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
        start = (p.get("start_time", "") or "")[:5]
        rec = p.get("recommendation", "")
        summary = (p.get("analysis_text") or "")[:200]

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

    try:
        await q.edit_message_text(_truncate_tg(txt), reply_markup=kb)
    except Exception:
        await q.message.reply_text(_truncate_tg(txt), reply_markup=kb)


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

    try:
        await q.edit_message_text(txt, reply_markup=kb)
    except Exception:
        await q.message.reply_text(txt, reply_markup=kb)


def kb_main_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🎯 Охотник", callback_data="MENU:HUNTER")],
        [InlineKeyboardButton("📊 Анализ матчей", callback_data="MENU:MATCHES")],
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
            "score": str(getattr(m, "score", "") or ""),
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


async def _render_sport_nav_root(user_id: int, sport_slug: str) -> Tuple[str, InlineKeyboardMarkup]:
    from ..integrations.sport_api import SportAPIClient, SportAPIError

    today = datetime.now(MSK).date()
    title = SPORT_LABELS.get(sport_slug, sport_slug)

    try:
        api = SportAPIClient()
        matches = await api.matches_by_date(sport_slug, today)
    except SportAPIError as e:
        text = (
            f"🏟 Матчи сегодня (по МСК) — {title}\n"
            f"Дата: {today.isoformat()}\n\n"
            "Не удалось получить матчи из API.\n"
            f"Причина: {str(e)[:250]}"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ К спорту", callback_data="BACK:MATCHES_MENU")]])
        return text, kb

    if not matches:
        text = (
            f"🏟 Матчи сегодня (по МСК) — {title}\n"
            f"Дата: {today.isoformat()}\n\n"
            "Сегодня нет запланированных матчей в доступных лигах.\n"
            "Попробуйте завтра или выберите другой спорт."
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
            "🌟 /pro — PRO подписка",
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
        await run_daily_hunter(bot=None)
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
                start = (m.start_time or "")[:5]  # HH:MM
                title = (m.title or "Матч")[:40]
                league = (m.league or "")[:20]
                lines.append(f"  {emoji} {start} {title} ({league})")
        lines.append("\n🔔 Подпишись на PRO — получай уведомления о начале матчей!")
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
            score = (m.score or "?:?")
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

    user_id = update.effective_user.id if update.effective_user else 0
    text_raw = (update.message.text or "").strip()
    norm = text_raw.lower().strip()

    logger.info("tg.handle_message user_id=%s text=%r", user_id, text_raw)

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

    # BACK:MENU
    if data == "BACK:MENU":
        try:
            await q.edit_message_text(MAIN_MENU_TEXT, reply_markup=kb_main_menu())
        except Exception:
            await q.message.reply_text(MAIN_MENU_TEXT, reply_markup=kb_main_menu())
        return

    # BUY:PRO — show tariff selection
    if data == "BUY:PRO":
        txt = _truncate_tg(_text_buy_pro(user_id))
        try:
            await q.edit_message_text(_safe_markdown(txt), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_buy_pro())
        except Exception:
            await q.message.reply_text(_safe_markdown(txt), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_buy_pro())
        return

    # PRO:<tariff_key> — временная заглушка (до подключения ЮKassa)
    if data.startswith("PRO:") and data != "PRO:TRIAL":
        tariff_key = data.split(":", 1)[1].strip().lower()
        try:
            from .payments import pro_stub_text, kb_pro_stub
            txt = pro_stub_text(tariff_key)
            kb = kb_pro_stub()
        except Exception:
            txt = "💳 Подключение оплаты в процессе.\nОплата станет доступна в ближайшее время!"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="MENU:PREMIUM")]])
        try:
            await q.edit_message_text(txt, reply_markup=kb)
        except Exception:
            await q.message.reply_text(txt, reply_markup=kb)
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
            await run_daily_hunter(bot=None)
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
                    start = (m.start_time or "")[:5]
                    title = (m.title or "Матч")[:40]
                    league = (m.league or "")[:20]
                    lines.append(f"  {emoji} {start} {title} ({league})")
            lines.append("\n🔔 Подпишись на PRO — получай уведомления о начале матчей!")
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
                score = (m.score or "?:?")
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
        txt = _truncate_tg(reply)

        try:
            await q.edit_message_text(
                _safe_markdown(txt),
                reply_markup=kb_match_hub(match_id),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            await q.message.reply_text(
                _safe_markdown(txt),
                reply_markup=kb_match_hub(match_id),
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    # UI actions
    if data.startswith("UI:"):
        parts = data.split(":")
        if len(parts) < 4:
            try:
                await q.edit_message_text("⚠️ Некорректная команда.", reply_markup=kb_main_menu())
            except Exception:
                await q.message.reply_text("⚠️ Некорректная команда.", reply_markup=kb_main_menu())
            return

        match_id = parts[1].strip()
        mode = parts[2].strip().lower()
        action = parts[3].strip().lower()

        reply = await call_agent_local(user_id, f"ui match {match_id} {mode} {action}")
        txt = _truncate_tg(reply)

        try:
            await q.edit_message_text(
                _safe_markdown(txt),
                reply_markup=kb_match_hub(match_id),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            await q.message.reply_text(
                _safe_markdown(txt),
                reply_markup=kb_match_hub(match_id),
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    # fallback
    try:
        await q.edit_message_text("Не понял действие. Открой меню.", reply_markup=kb_main_menu())
    except Exception:
        await q.message.reply_text("Не понял действие. Открой меню.", reply_markup=kb_main_menu())


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


@telegram_router.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    payload = await request.json()

    if _telegram_app is None:
        logger.error("Telegram webhook received, but PTB app is not initialized (_telegram_app is None)")
        return JSONResponse({"ok": False, "error": "ptb_not_initialized"}, status_code=503)

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
