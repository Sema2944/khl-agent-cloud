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

from ..db import get_session
from ..pro_db import is_pro
from ..ui_text import MAIN_MENU_TEXT
from ..user_access import allowed_sports_for_user

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

SPORT_LABELS = {
    "ice-hockey": "🏒 Хоккей",
    "football": "⚽ Футбол",
    "basketball": "🏀 Баскетбол",
    "tennis": "🎾 Теннис",
    "table-tennis": "🏓 Настольный теннис",
    "esports": "🎮 Киберспорт",
}

DEFAULT_SPORTS = ["ice-hockey", "football", "basketball", "tennis", "table-tennis", "esports"]


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

    is_live = st in {"live", "inprogress", "in_progress"}
    is_done = st in {"finished", "ended"}
    is_ns = st in {"notstarted", "not_started", "scheduled"}

    prefix = ""
    if is_live:
        prefix = "🟢 "
    elif is_done:
        prefix = "✅ "
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
    return (
        "⭐ Premium\n\n"
        "Что входит:\n"
        "• LIVE PRO в матчах\n"
        "• Больше аналитики\n\n"
        "Нажми кнопку ниже, чтобы оформить."
    )


def kb_main_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🏟 Матчи сегодня", callback_data="MENU:MATCHES")],
        [InlineKeyboardButton("🧠 AI Аналитика", callback_data="MENU:AI")],
        [InlineKeyboardButton("👤 Стратегия эксперта", callback_data="MENU:STRATEGY")],
        [InlineKeyboardButton("📊 Профиль", callback_data="MENU:PROFILE")],
        [InlineKeyboardButton("⭐ Premium", callback_data="MENU:PREMIUM")],
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
    return (league or "").strip() or "Other"


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

        country = (getattr(m, "country", "") or "").strip() or "Other"
        league_raw = (getattr(m, "league", "") or "").strip() or "Other"
        league = _league_ru(league_raw)

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

    for key, ids in match_ids_by_league.items():

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
        for (ck, lk), ids in st.match_ids_by_league.items():
            if ck == ckey:
                n += len(ids)
        counts.append((ckey, n))
    counts.sort(key=lambda x: x[1], reverse=True)

    buf: List[InlineKeyboardButton] = []
    for ckey, n in counts[:18]:
        cname = st.country_by_key.get(ckey, "Other")
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
    chunk = ids[start : start + _PER_PAGE]

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
    country = st.country_by_key.get(ckey, "Other")
    return f"🏳️ Страна: {country}\n\nВыбери лигу:"


def _text_matches(user_id: int, ckey: str, lkey: str, page: int) -> str:
    st = _NAV_BY_USER.get(user_id)
    if not st:
        return "Нет данных."
    country = st.country_by_key.get(ckey, "Other")
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
    # временная заглушка, чтобы бот не падал из-за payments.py
    rows = [
        [InlineKeyboardButton("⭐ Оформить Premium (скоро)", callback_data="BUY:PRO")],
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
    text = "Привет! Выбери раздел 👇"
    if update.message:
        await update.message.reply_text(text, reply_markup=kb_main_menu())


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    user_id = update.effective_user.id if update.effective_user else 0
    text_raw = (update.message.text or "").strip()
    norm = text_raw.lower().strip()

    logger.info("tg.handle_message user_id=%s text=%r", user_id, text_raw)

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

    # BUY:PRO (пока без платежей — показываем инструкцию)
    if data == "BUY:PRO":
        txt = _truncate_tg(_text_buy_pro(user_id))
        try:
            await q.edit_message_text(_safe_markdown(txt), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_buy_pro())
        except Exception:
            await q.message.reply_text(_safe_markdown(txt), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_buy_pro())
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

    # MENU shortcuts
    if data == "MENU:AI":
        reply = (
            "Как пользоваться:\n"
            "1) 🏟 Матчи сегодня\n"
            "2) спорт → страна → лига → матч\n"
            "3) в матче нажми: PRE / LIVE / LIVE PRO\n\n"
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
