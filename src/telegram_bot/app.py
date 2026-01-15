# src/telegram_bot/app.py
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")


# ============================================================
# ENV
# ============================================================
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").strip()  # https://xxxx.onrender.com
WEBHOOK_PATH = (os.getenv("TELEGRAM_WEBHOOK_PATH") or "/telegram/webhook").strip()
WEBHOOK_URL = (os.getenv("TELEGRAM_WEBHOOK_URL") or "").strip()  # если задан — используем, иначе PUBLIC_URL + WEBHOOK_PATH

# доступ по тарифу
ALLOWED_SPORTS = [s.strip() for s in (os.getenv("ALLOWED_SPORTS") or "ice-hockey").split(",") if s.strip()]
HIDE_LOCKED_SPORTS = (os.getenv("HIDE_LOCKED_SPORTS") or "").strip().lower() in {"1", "true", "yes", "on"}

# лимит текста под Telegram (защита от Message_too_long)
TG_TEXT_LIMIT = 3800

# навигация
LEAGUES_PER_PAGE = 16   # сколько лиг на странице
MATCHES_PER_PAGE = 14   # сколько матчей на странице


# ============================================================
# UI labels
# ============================================================
SPORT_LABELS = {
    "ice-hockey": "🏒 Хоккей",
    "football": "⚽️ Футбол",
    "basketball": "🏀 Баскетбол",
    "tennis": "🎾 Теннис",
    "table-tennis": "🏓 Настольный теннис",
    "esports": "🎮 Киберспорт",
}

MAIN_MENU_TEXT = "Главное меню"


# ============================================================
# Telegram Application (создаётся в telegram_startup)
# ============================================================
_telegram_app: Optional[Application] = None
router = APIRouter()


# ============================================================
# Helpers
# ============================================================
def _is_allowed_sport(sport_slug: str) -> bool:
    s = (sport_slug or "").strip().lower()
    return s in {x.lower() for x in ALLOWED_SPORTS}


def _msk_today_iso() -> str:
    return datetime.now(MSK).date().isoformat()


def _safe_markdown(text: str) -> str:
    """
    Минимальная экранизация под Markdown (ParseMode.MARKDOWN).
    """
    s = text or ""
    s = s.replace("\\", "\\\\")
    s = s.replace("_", "\\_").replace("*", "\\*").replace("[", "\\[")
    s = s.replace("`", "\\`")
    return s


def _truncate_tg(text: str, limit: int = TG_TEXT_LIMIT) -> str:
    t = text or ""
    if len(t) <= limit:
        return t
    return t[: limit - 50] + "\n\n…(сообщение обрезано)"


def _short_key(s: str, n: int = 10) -> str:
    h = hashlib.sha1((s or "").encode("utf-8")).hexdigest()
    return h[:n]


def _parse_time_msk(start_time: str) -> str:
    """
    Пытаемся вытащить HH:MM по МСК из ISO.
    Если не получилось — возвращаем пусто.
    """
    if not start_time:
        return ""
    s = str(start_time).strip()
    if not s:
        return ""
    # частый случай: ...Z
    s2 = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            # если пришло без TZ — считаем что UTC
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        dt = dt.astimezone(MSK)
        return dt.strftime("%H:%M")
    except Exception:
        return ""


async def call_agent_local(user_id: int, text: str) -> str:
    """
    Вызываем локального агента (src/parsing.py).
    """
    from ..parsing import run_dialog_agent  # локальный импорт, чтобы избежать циклов
    return await run_dialog_agent(user_id, text)


# ============================================================
# Keyboards
# ============================================================
def kb_main_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🏟 Матчи сегодня", callback_data="MENU:MATCHES")],
        [InlineKeyboardButton("🧠 AI Аналитика", callback_data="MENU:AI")],
        [InlineKeyboardButton("👤 Стратегия эксперта", callback_data="MENU:STRATEGY")],
        [InlineKeyboardButton("📊 Профиль", callback_data="MENU:PROFILE")],
        [InlineKeyboardButton("⭐ Премиум", callback_data="MENU:PREMIUM")],
    ]
    return InlineKeyboardMarkup(rows)


def kb_sports() -> InlineKeyboardMarkup:
    """
    Меню выбора спорта:
    - доступные: SPORT:<slug>
    - недоступные: SPORT_LOCKED:<slug> (или скрываем)
    """
    rows: List[List[InlineKeyboardButton]] = []

    for slug in ["ice-hockey", "football", "basketball", "tennis", "table-tennis", "esports"]:
        title = SPORT_LABELS.get(slug, slug)
        if _is_allowed_sport(slug):
            rows.append([InlineKeyboardButton(title, callback_data=f"SPORT:{slug}")])
        else:
            if HIDE_LOCKED_SPORTS:
                continue
            rows.append([InlineKeyboardButton(f"🔒 {title}", callback_data=f"SPORT_LOCKED:{slug}")])

    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="BACK:MENU")])
    return InlineKeyboardMarkup(rows)


def kb_match_hub(match_id: str) -> InlineKeyboardMarkup:
    """
    Клавиатура внутри матча: UI:<match_id>:<pre|live>:<action>
    """
    mid = str(match_id).strip()
    rows = [
        [
            InlineKeyboardButton("📊 Обзор (PRE)", callback_data=f"UI:{mid}:pre:overview"),
            InlineKeyboardButton("🟢 LIVE", callback_data=f"UI:{mid}:live:overview"),
        ],
        [
            InlineKeyboardButton("1X2", callback_data=f"UI:{mid}:pre:moneyline"),
            InlineKeyboardButton("Тотал", callback_data=f"UI:{mid}:pre:total"),
            InlineKeyboardButton("Фора", callback_data=f"UI:{mid}:pre:handicap"),
        ],
        [InlineKeyboardButton("🔄 Обновить LIVE", callback_data=f"UI:{mid}:live:refresh")],
        [InlineKeyboardButton("⬅️ Назад к матчам", callback_data="BACK:MATCHES_MENU")],
        [InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")],
    ]
    return InlineKeyboardMarkup(rows)


# ============================================================
# Navigation: League -> Matches (paged)
# ============================================================
@dataclass
class _LeagueNavState:
    sport: str
    today_iso: str
    league_by_key: Dict[str, Dict[str, str]]  # lkey -> {"league":..,"country":..}
    match_ids_by_league: Dict[str, List[str]]  # lkey -> [match_id..]
    match_meta: Dict[str, Dict[str, str]]  # match_id -> meta


_NAV_BY_USER: Dict[int, _LeagueNavState] = {}


def _norm_country(country: str) -> str:
    c = (country or "").strip()
    if not c or c.lower() in {"other", "unknown", "none", "null"}:
        return "Other"
    return c


def _norm_league(league: str) -> str:
    l = (league or "").strip()
    if not l or l.lower() in {"other", "unknown", "none", "null"}:
        return "Other"
    return l


def _build_league_nav_state(user_id: int, sport_slug: str, matches: List[Any]) -> _LeagueNavState:
    today_iso = _msk_today_iso()

    league_by_key: Dict[str, Dict[str, str]] = {}
    match_ids_by_league: Dict[str, List[str]] = {}
    match_meta: Dict[str, Dict[str, str]] = {}

    for m in matches:
        mid = str(getattr(m, "id", "") or "").strip()
        if not mid:
            continue

        league = _norm_league(str(getattr(m, "league", "") or ""))
        country = _norm_country(str(getattr(m, "country", "") or ""))

        # ключ лиги — устойчивый и короткий
        lkey = _short_key(f"{league}::{country}", n=10)

        league_by_key[lkey] = {"league": league, "country": country}
        match_ids_by_league.setdefault(lkey, []).append(mid)

        match_meta[mid] = {
            "title": str(getattr(m, "title", "") or f"Матч {mid}"),
            "league": league,
            "country": country,
            "status": str(getattr(m, "status", "") or ""),
            "score": str(getattr(m, "score", "") or ""),
            "start_time": str(getattr(m, "start_time", "") or ""),
        }

    # сортировка матчей внутри лиги — по времени (строкой ISO)
    for lkey, ids in match_ids_by_league.items():
        ids.sort(key=lambda _mid: (match_meta.get(_mid) or {}).get("start_time") or "")

    return _LeagueNavState(
        sport=sport_slug,
        today_iso=today_iso,
        league_by_key=league_by_key,
        match_ids_by_league=match_ids_by_league,
        match_meta=match_meta,
    )


def _league_label(league: str, country: str) -> str:
    """
    Как показываем лигу пользователю.
    """
    league = league or "Other"
    country = country or "Other"
    if league == "Other" and country == "Other":
        return "Другие"
    if country == "Other":
        return league
    if league == "Other":
        return f"{country} • Другие"
    return f"{league} • {country}"


def _kb_leagues(user_id: int, sport_slug: str, page: int) -> InlineKeyboardMarkup:
    st = _NAV_BY_USER.get(user_id)
    rows: List[List[InlineKeyboardButton]] = []

    if not st:
        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="BACK:MATCHES_MENU")])
        return InlineKeyboardMarkup(rows)

    items: List[Tuple[str, int]] = []
    for lkey, ids in st.match_ids_by_league.items():
        info = st.league_by_key.get(lkey) or {"league": "Other", "country": "Other"}
        league = info.get("league", "Other")
        country = info.get("country", "Other")
        # "Other/Other" уводим вниз
        weight = 1 if (league == "Other" and country == "Other") else 0
        items.append((lkey, len(ids) * 1000 - weight))
    items.sort(key=lambda x: x[1], reverse=True)

    total = len(items)
    pages = max(1, (total + LEAGUES_PER_PAGE - 1) // LEAGUES_PER_PAGE)
    page = max(1, min(page, pages))

    start = (page - 1) * LEAGUES_PER_PAGE
    chunk = items[start : start + LEAGUES_PER_PAGE]

    for lkey, score in chunk:
        n = max(0, score // 1000)
        info = st.league_by_key.get(lkey) or {"league": "Other", "country": "Other"}
        label = _league_label(info.get("league", "Other"), info.get("country", "Other"))
        # коротко
        btn = label
        if len(btn) > 42:
            btn = btn[:41] + "…"
        rows.append([InlineKeyboardButton(f"{btn} ({n})", callback_data=f"NAV:LEAGUE:{sport_slug}:{lkey}:1")])

    nav_row: List[InlineKeyboardButton] = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"NAV:LEAGUES_PAGE:{sport_slug}:{page-1}"))
    nav_row.append(InlineKeyboardButton(f"{page}/{pages}", callback_data="NOOP"))
    if page < pages:
        nav_row.append(InlineKeyboardButton("➡️", callback_data=f"NAV:LEAGUES_PAGE:{sport_slug}:{page+1}"))
    rows.append(nav_row)

    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="BACK:MATCHES_MENU")])
    return InlineKeyboardMarkup(rows)


def _kb_matches(user_id: int, sport_slug: str, lkey: str, page: int) -> InlineKeyboardMarkup:
    st = _NAV_BY_USER.get(user_id)
    rows: List[List[InlineKeyboardButton]] = []
    if not st:
        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="BACK:MATCHES_MENU")])
        return InlineKeyboardMarkup(rows)

    ids = st.match_ids_by_league.get(lkey, [])
    total = len(ids)
    pages = max(1, (total + MATCHES_PER_PAGE - 1) // MATCHES_PER_PAGE)
    page = max(1, min(page, pages))

    start = (page - 1) * MATCHES_PER_PAGE
    chunk = ids[start : start + MATCHES_PER_PAGE]

    for mid in chunk:
        meta = st.match_meta.get(mid) or {}
        title = meta.get("title") or f"Матч {mid}"
        status = (meta.get("status") or "").lower()
        score = meta.get("score") or ""
        tm = _parse_time_msk(meta.get("start_time") or "")

        badge = ""
        if status in {"live", "inprogress", "in_progress"}:
            badge = "🟢 "
        elif status in {"finished", "ended"}:
            badge = "✅ "
        elif status in {"canceled", "cancelled"}:
            badge = "⛔ "

        # строка кнопки: "19:45 🟢 Team — Team (0:0)"
        btn = title
        if tm:
            btn = f"{tm} {btn}"
        if score:
            btn = f"{btn} ({score})"
        btn = f"{badge}{btn}".strip()

        if len(btn) > 58:
            btn = btn[:57] + "…"

        rows.append([InlineKeyboardButton(btn, callback_data=f"MATCH:{sport_slug}:{mid}")])

    nav_row: List[InlineKeyboardButton] = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"NAV:MATCHES_PAGE:{sport_slug}:{lkey}:{page-1}"))
    nav_row.append(InlineKeyboardButton(f"{page}/{pages}", callback_data="NOOP"))
    if page < pages:
        nav_row.append(InlineKeyboardButton("➡️", callback_data=f"NAV:MATCHES_PAGE:{sport_slug}:{lkey}:{page+1}"))
    rows.append(nav_row)

    rows.append([InlineKeyboardButton("⬅️ Лиги", callback_data=f"BACK:LEAGUES:{sport_slug}:1")])
    rows.append([InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")])
    return InlineKeyboardMarkup(rows)


def _text_leagues(user_id: int, sport_slug: str, page: int) -> str:
    title = SPORT_LABELS.get(sport_slug, sport_slug)
    st = _NAV_BY_USER.get(user_id)
    today_iso = st.today_iso if st else _msk_today_iso()

    total_leagues = len(st.match_ids_by_league) if st else 0
    pages = max(1, (total_leagues + LEAGUES_PER_PAGE - 1) // LEAGUES_PER_PAGE)
    page = max(1, min(page, pages))

    return (
        f"🏟 Матчи сегодня (по МСК) — {title}\n"
        f"Дата: {today_iso}\n\n"
        f"Выбери лигу:\n"
        f"Страница {page}/{pages}"
    )


def _text_matches(user_id: int, lkey: str, page: int) -> str:
    st = _NAV_BY_USER.get(user_id)
    if not st:
        return "Нет данных."

    info = st.league_by_key.get(lkey) or {"league": "Other", "country": "Other"}
    label = _league_label(info.get("league", "Other"), info.get("country", "Other"))

    ids = st.match_ids_by_league.get(lkey, [])
    total = len(ids)
    pages = max(1, (total + MATCHES_PER_PAGE - 1) // MATCHES_PER_PAGE)
    page = max(1, min(page, pages))

    return (
        f"🏆 {label}\n"
        f"Матчи: {total} • Страница {page}/{pages}\n\n"
        "Нажми матч ниже 👇"
    )


async def _render_sport_leagues_root(user_id: int, sport_slug: str, page: int = 1) -> Tuple[str, InlineKeyboardMarkup]:
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
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="BACK:MATCHES_MENU")]])
        return text, kb

    _NAV_BY_USER[user_id] = _build_league_nav_state(user_id, sport_slug, matches)
    return _text_leagues(user_id, sport_slug, page), _kb_leagues(user_id, sport_slug, page)


# ============================================================
# Handlers
# ============================================================
async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = "Привет! Выбери раздел 👇"
    await update.message.reply_text(text, reply_markup=kb_main_menu())


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    text_raw = (update.message.text or "").strip()
    norm = text_raw.lower()

    logger.info("tg.handle_message user_id=%s text=%r", user_id, text_raw)

    # быстрый вход в матчи
    if "матчи сегодня" in norm:
        await update.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
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

    # BACK
    if data == "BACK:MENU":
        try:
            await q.edit_message_text(MAIN_MENU_TEXT, reply_markup=kb_main_menu())
        except Exception:
            await q.message.reply_text(MAIN_MENU_TEXT, reply_markup=kb_main_menu())
        return

    if data in {"MENU:MATCHES", "BACK:MATCHES_MENU"}:
        text = "🏟 Выбери спорт:"
        try:
            await q.edit_message_text(text, reply_markup=kb_sports())
        except Exception:
            await q.message.reply_text(text, reply_markup=kb_sports())
        return

    # MENU shortcuts
    if data == "MENU:AI":
        reply = (
            "Как пользоваться:\n"
            "1) 🏟 Матчи сегодня\n"
            "2) спорт → лига → матч\n"
            "3) в матче нажми: PRE / LIVE / рынки\n\n"
            "Диагностика: llm ping, env, version, last_error"
        )
        try:
            await q.edit_message_text(reply, reply_markup=kb_main_menu())
        except Exception:
            await q.message.reply_text(reply, reply_markup=kb_main_menu())
        return

    if data == "MENU:STRATEGY":
        reply = await call_agent_local(user_id, "стратегия")
        txt = _truncate_tg(reply)
        try:
            await q.edit_message_text(_safe_markdown(txt), reply_markup=kb_main_menu(), parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await q.message.reply_text(_safe_markdown(txt), reply_markup=kb_main_menu(), parse_mode=ParseMode.MARKDOWN)
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
        from ..ui_text import text_premium

        try:
            await q.edit_message_text(text_premium(), reply_markup=kb_main_menu())
        except Exception:
            await q.message.reply_text(text_premium(), reply_markup=kb_main_menu())
        return

    # SPORT locked
    if data.startswith("SPORT_LOCKED:"):
        slug = data.split(":", 1)[1].strip().lower()
        title = SPORT_LABELS.get(slug, slug)
        txt = (
            f"🔒 {title} недоступен по твоему тарифу.\n\n"
            "Сейчас доступно: " + ", ".join(SPORT_LABELS.get(s, s) for s in ALLOWED_SPORTS)
        )
        try:
            await q.edit_message_text(txt, reply_markup=kb_sports())
        except Exception:
            await q.message.reply_text(txt, reply_markup=kb_sports())
        return

    # SPORT selection (только разрешённые)
    if data.startswith("SPORT:"):
        sport_slug = data.split(":", 1)[1].strip().lower()
        if not _is_allowed_sport(sport_slug):
            title = SPORT_LABELS.get(sport_slug, sport_slug)
            txt = f"🔒 {title} недоступен по твоему тарифу."
            try:
                await q.edit_message_text(txt, reply_markup=kb_sports())
            except Exception:
                await q.message.reply_text(txt, reply_markup=kb_sports())
            return

        text, kb = await _render_sport_leagues_root(user_id, sport_slug, page=1)
        txt = _truncate_tg(text)
        try:
            await q.edit_message_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await q.message.reply_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    # NAV: leagues pages
    if data.startswith("NAV:LEAGUES_PAGE:"):
        # NAV:LEAGUES_PAGE:<sport>:<page>
        parts = data.split(":")
        if len(parts) < 4:
            return
        sport_slug = parts[2].strip().lower()
        try:
            page = int(parts[3].strip())
        except Exception:
            page = 1

        st = _NAV_BY_USER.get(user_id)
        # если стейта нет — перезагрузим
        if not st or st.sport != sport_slug:
            text, kb = await _render_sport_leagues_root(user_id, sport_slug, page=page)
        else:
            text = _text_leagues(user_id, sport_slug, page=page)
            kb = _kb_leagues(user_id, sport_slug, page=page)

        txt = _truncate_tg(text)
        try:
            await q.edit_message_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await q.message.reply_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    # NAV: open league (page 1 default)
    if data.startswith("NAV:LEAGUE:"):
        # NAV:LEAGUE:<sport>:<lkey>:<page>
        parts = data.split(":")
        if len(parts) < 5:
            return
        sport_slug = parts[2].strip().lower()
        lkey = parts[3].strip()
        try:
            page = int(parts[4].strip())
        except Exception:
            page = 1

        text = _text_matches(user_id, lkey, page=page)
        kb = _kb_matches(user_id, sport_slug, lkey, page=page)

        txt = _truncate_tg(text)
        try:
            await q.edit_message_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await q.message.reply_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    # NAV: matches page
    if data.startswith("NAV:MATCHES_PAGE:"):
        # NAV:MATCHES_PAGE:<sport>:<lkey>:<page>
        parts = data.split(":")
        if len(parts) < 5:
            return
        sport_slug = parts[2].strip().lower()
        lkey = parts[3].strip()
        try:
            page = int(parts[4].strip())
        except Exception:
            page = 1

        text = _text_matches(user_id, lkey, page=page)
        kb = _kb_matches(user_id, sport_slug, lkey, page=page)

        txt = _truncate_tg(text)
        try:
            await q.edit_message_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await q.message.reply_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    # BACK to leagues
    if data.startswith("BACK:LEAGUES:"):
        # BACK:LEAGUES:<sport>:<page>
        parts = data.split(":")
        if len(parts) < 4:
            return
        sport_slug = parts[2].strip().lower()
        try:
            page = int(parts[3].strip())
        except Exception:
            page = 1

        text = _text_leagues(user_id, sport_slug, page=page)
        kb = _kb_leagues(user_id, sport_slug, page=page)

        txt = _truncate_tg(text)
        try:
            await q.edit_message_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await q.message.reply_text(_safe_markdown(txt), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    # MATCH open
    if data.startswith("MATCH:"):
        # MATCH:<sport_slug>:<match_id>
        parts = data.split(":")
        if len(parts) >= 3:
            sport_slug = parts[1].strip().lower()
            match_id = ":".join(parts[2:]).strip()
        else:
            sport_slug = "ice-hockey"
            match_id = data.split(":", 1)[1].strip()

        # прогреваем контекст для parsing.py (кеш матчей на день)
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
        # UI:<match_id>:<pre|live>:<action>
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


# ============================================================
# Telegram init / webhook
# ============================================================
def create_application() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app


async def telegram_startup() -> None:
    """
    Вызывай это из src/service.py на старте.
    """
    global _telegram_app
    if _telegram_app is not None:
        return

    _telegram_app = create_application()

    await _telegram_app.initialize()
    await _telegram_app.start()

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
# FastAPI webhook router
# ============================================================
@router.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request) -> Dict[str, Any]:
    """
    FastAPI endpoint для Telegram webhook.
    """
    if _telegram_app is None:
        await telegram_startup()

    data = await request.json()
    update = Update.de_json(data, _telegram_app.bot)  # type: ignore[arg-type]
    await _telegram_app.process_update(update)  # type: ignore[union-attr]
    return {"ok": True}
