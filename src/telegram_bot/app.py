# src/telegram_bot/app.py
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, FastAPI, Request, Response
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# ENV
# ---------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").strip().rstrip("/")
WEBHOOK_PATH = (os.getenv("TELEGRAM_WEBHOOK_PATH") or "/telegram/webhook").strip()

# Если PUBLIC_URL не задан, webhook не поставим (но приложение поднимется)
WEBHOOK_URL = f"{PUBLIC_URL}{WEBHOOK_PATH}" if PUBLIC_URL else ""

# Ограничения (можно вынести в env)
MAX_MATCHES_IN_LIST = int(os.getenv("TG_MAX_MATCHES_IN_LIST") or "30")
MAX_ROWS_PER_KB = int(os.getenv("TG_MAX_ROWS_PER_KB") or "8")

# ---------------------------------------------------------------------
# Глобальное состояние для webhook PTB
# ---------------------------------------------------------------------
_tg_app: Optional[Application] = None
_router: Optional[APIRouter] = None

# кэш сообщений "список матчей" по пользователю, чтобы BACK работал предсказуемо
_LAST_SPORT_BY_USER: Dict[int, str] = {}
_LAST_MATCH_LIST_TEXT_BY_USER: Dict[int, str] = {}
_LAST_MATCH_LIST_KB_BY_USER: Dict[int, InlineKeyboardMarkup] = {}

# ---------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------
SPORTS: List[Tuple[str, str]] = [
    ("football", "⚽ Футбол"),
    ("ice-hockey", "🏒 Хоккей"),
    ("basketball", "🏀 Баскетбол"),
    ("tennis", "🎾 Теннис"),
    ("table-tennis", "🏓 Настольный теннис"),
    ("esports", "🎮 Киберспорт"),
]


def kb_main_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🏟 Матчи сегодня", callback_data="MENU:MATCHES")],
        [
            InlineKeyboardButton("🧠 AI Аналитика", callback_data="MENU:AI"),
            InlineKeyboardButton("👤 Стратегия эксперта", callback_data="MENU:STRATEGY"),
        ],
        [
            InlineKeyboardButton("📊 Профиль", callback_data="MENU:PROFILE"),
            InlineKeyboardButton("⭐ Premium", callback_data="MENU:PREMIUM"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def kb_sports() -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for slug, label in SPORTS:
        rows.append([InlineKeyboardButton(label, callback_data=f"SPORT:{slug}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="BACK:MENU")])
    return InlineKeyboardMarkup(rows)


def kb_match_hub(match_id: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("📊 Pre", callback_data=f"UI:{match_id}:pre:overview"),
            InlineKeyboardButton("🟢 LIVE", callback_data=f"UI:{match_id}:live:overview"),
        ],
        [
            InlineKeyboardButton("🧠 1X2", callback_data=f"UI:{match_id}:pre:moneyline"),
            InlineKeyboardButton("🧠 Тотал", callback_data=f"UI:{match_id}:pre:total"),
            InlineKeyboardButton("🧠 Фора", callback_data=f"UI:{match_id}:pre:handicap"),
        ],
        [
            InlineKeyboardButton("🔗 Связки", callback_data=f"UI:{match_id}:pre:links"),
            InlineKeyboardButton("🔄 Обновить LIVE", callback_data=f"UI:{match_id}:live:refresh"),
        ],
        [InlineKeyboardButton("⬅️ К матчам", callback_data="BACK:MATCHES")],
        [InlineKeyboardButton("🏠 Меню", callback_data="BACK:MENU")],
    ]
    return InlineKeyboardMarkup(rows)


def kb_matches_list(sport_slug: str, match_ids: List[str]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for mid in match_ids[:MAX_MATCHES_IN_LIST]:
        rows.append([InlineKeyboardButton(f"Матч {mid}", callback_data=f"MATCH:{sport_slug}:{mid}")])

    # ограничим клаву по рядам, чтобы не упереться в лимиты
    if len(rows) > MAX_MATCHES_IN_LIST:
        rows = rows[:MAX_MATCHES_IN_LIST]

    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="BACK:MATCHES_MENU")])
    rows.append([InlineKeyboardButton("🏠 Меню", callback_data="BACK:MENU")])
    return InlineKeyboardMarkup(rows)


def _safe_markdown(text: str) -> str:
    # parsing.py уже md-safe экранирует, но на всякий случай не будем ломать сообщение
    return text or ""


# ---------------------------------------------------------------------
# Agent bridge
# ---------------------------------------------------------------------
async def call_agent_local(user_id: int, text: str) -> str:
    """
    ВАЖНО: импортируем run_dialog_agent внутри функции, чтобы
    не словить ImportError на старте приложения (и облегчить hotfix).
    """
    from ..parsing import run_dialog_agent

    return await run_dialog_agent(user_id, text)


# ---------------------------------------------------------------------
# Helpers: fetch match list from API to build better buttons
# ---------------------------------------------------------------------
async def _fetch_matches_for_buttons(sport_slug: str) -> List[str]:
    """
    Пытаемся получить реальные match_id через SportAPIClient.
    Если API не настроен/упал — вернём пусто, тогда кнопки будут только спорт.
    """
    try:
        from ..integrations.sport_api import SportAPIClient

        api = SportAPIClient()
        today = datetime.now().date()  # в API у нас date=YYYY-MM-DD; зона не критична для кнопок
        matches = await api.matches_by_date(sport_slug, today)
        return [m.id for m in matches if getattr(m, "id", None)]
    except Exception:
        logger.exception("Cannot fetch matches for buttons")
        return []


async def _render_sport_matches_screen(user_id: int, sport_slug: str) -> Tuple[str, InlineKeyboardMarkup]:
    """
    1) зовём агента: это заполнит кеши матчей/контекст в parsing.py
    2) параллельно пытаемся получить id матчей из API для кнопок
    """
    agent_text = await call_agent_local(user_id, f"матчи сегодня {sport_slug}")
    ids = await _fetch_matches_for_buttons(sport_slug)

    # Если API вернул ids — сделаем кнопки матчей.
    # Если нет — покажем хотя бы "матч <id>" через текст агента (там id есть).
    if ids:
        kb = kb_matches_list(sport_slug, ids)
    else:
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⬅️ Назад", callback_data="BACK:MATCHES_MENU")],
                [InlineKeyboardButton("🏠 Меню", callback_data="BACK:MENU")],
            ]
        )

    return agent_text, kb


# ---------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    user_id = update.effective_user.id if update.effective_user else 0
    text = (update.message.text or "").strip()
    logger.info("tg.handle_message user_id=%s text=%r", user_id, text)

    # UX: имитируем "печатает"
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except Exception:
        pass

    # Быстрые кнопочные команды (если пользователь шлёт текстом)
    if text in {"🏟 Матчи сегодня", "/matches"}:
        await update.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
        return

    if text in {"🧠 AI Аналитика", "/ai"}:
        reply = (
            "Как пользоваться:\n"
            "1) 🏟 Матчи сегодня\n"
            "2) спорт → матч\n"
            "3) в матче нажми: Pre / LIVE / рынки\n\n"
            "Диагностика: llm ping, env, version, last_error"
        )
        await update.message.reply_text(reply, reply_markup=kb_main_menu())
        return

    if text in {"👤 Стратегия эксперта", "/strategy"}:
        reply = await call_agent_local(user_id, "стратегия")
        await update.message.reply_text(_safe_markdown(reply), reply_markup=kb_main_menu(), parse_mode=ParseMode.MARKDOWN)
        return

    if text in {"📊 Профиль", "/profile"}:
        reply = await call_agent_local(user_id, "профиль")
        await update.message.reply_text(_safe_markdown(reply), reply_markup=kb_main_menu(), parse_mode=ParseMode.MARKDOWN)
        return

    if text in {"⭐ Premium", "/premium"}:
        # premium делаем локально, чтобы не зависеть от агента
        from ..ui_text import text_premium

        await update.message.reply_text(text_premium(), reply_markup=kb_main_menu())
        return

    # Всё остальное — прокидываем в агента (в том числе “матч <id>”, “мой банк …”, “env” и т.п.)
    reply = await call_agent_local(user_id, text)
    await update.message.reply_text(_safe_markdown(reply), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main_menu())


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

    # BACK
    if data == "BACK:MENU":
        text = "Главное меню"
        try:
            await q.edit_message_text(text, reply_markup=kb_main_menu())
        except Exception:
            await q.message.reply_text(text, reply_markup=kb_main_menu())
        return

    if data == "MENU:MATCHES" or data == "BACK:MATCHES_MENU":
        text = "🏟 Выбери спорт:"
        try:
            await q.edit_message_text(text, reply_markup=kb_sports())
        except Exception:
            await q.message.reply_text(text, reply_markup=kb_sports())
        return

    if data == "BACK:MATCHES":
        # возвращаем последний список матчей, который показывали пользователю
        text = _LAST_MATCH_LIST_TEXT_BY_USER.get(user_id) or "🏟 Выбери спорт:"
        kb = _LAST_MATCH_LIST_KB_BY_USER.get(user_id) or kb_sports()
        try:
            await q.edit_message_text(_safe_markdown(text), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await q.message.reply_text(_safe_markdown(text), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    # MENU shortcuts
    if data == "MENU:AI":
        reply = (
            "Как пользоваться:\n"
            "1) 🏟 Матчи сегодня\n"
            "2) спорт → матч\n"
            "3) в матче нажми: Pre / LIVE / рынки\n\n"
            "Диагностика: llm ping, env, version, last_error"
        )
        try:
            await q.edit_message_text(reply, reply_markup=kb_main_menu())
        except Exception:
            await q.message.reply_text(reply, reply_markup=kb_main_menu())
        return

    if data == "MENU:STRATEGY":
        reply = await call_agent_local(user_id, "стратегия")
        try:
            await q.edit_message_text(_safe_markdown(reply), reply_markup=kb_main_menu(), parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await q.message.reply_text(_safe_markdown(reply), reply_markup=kb_main_menu(), parse_mode=ParseMode.MARKDOWN)
        return

    if data == "MENU:PROFILE":
        reply = await call_agent_local(user_id, "профиль")
        try:
            await q.edit_message_text(_safe_markdown(reply), reply_markup=kb_main_menu(), parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await q.message.reply_text(_safe_markdown(reply), reply_markup=kb_main_menu(), parse_mode=ParseMode.MARKDOWN)
        return

    if data == "MENU:PREMIUM":
        from ..ui_text import text_premium

        try:
            await q.edit_message_text(text_premium(), reply_markup=kb_main_menu())
        except Exception:
            await q.message.reply_text(text_premium(), reply_markup=kb_main_menu())
        return

    # SPORT selection
    if data.startswith("SPORT:"):
        sport_slug = data.split(":", 1)[1].strip()
        _LAST_SPORT_BY_USER[user_id] = sport_slug

        text, kb = await _render_sport_matches_screen(user_id, sport_slug)
        _LAST_MATCH_LIST_TEXT_BY_USER[user_id] = text
        _LAST_MATCH_LIST_KB_BY_USER[user_id] = kb

        try:
            await q.edit_message_text(_safe_markdown(text), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await q.message.reply_text(_safe_markdown(text), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    # MATCH open
    if data.startswith("MATCH:"):
        # MATCH:<sport_slug>:<match_id>
        parts = data.split(":")
        if len(parts) >= 3:
            sport_slug = parts[1].strip()
            match_id = ":".join(parts[2:]).strip()
        else:
            sport_slug = _LAST_SPORT_BY_USER.get(user_id, "")
            match_id = data.split(":", 1)[1].strip()

        # важно: сначала вызовем "матчи сегодня sport", чтобы parsing.py знал sport (и матч закешировался)
        if sport_slug:
            try:
                await call_agent_local(user_id, f"матчи сегодня {sport_slug}")
            except Exception:
                # не критично
                logger.exception("pre-cache matches failed")

        reply = await call_agent_local(user_id, f"матч {match_id}")

        try:
            await q.edit_message_text(
                _safe_markdown(reply),
                reply_markup=kb_match_hub(match_id),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            await q.message.reply_text(
                _safe_markdown(reply),
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
                await q.edit_message_text("⚠️ Некорректная команда.")
            except Exception:
                await q.message.reply_text("⚠️ Некорректная команда.")
            return

        match_id = parts[1].strip()
        mode = parts[2].strip().lower()
        action = parts[3].strip().lower()

        # parsing.py уже поддерживает "ui match <id> <mode> <action>"
        reply = await call_agent_local(user_id, f"ui match {match_id} {mode} {action}")

        # Оставляем клавиатуру хаба, чтобы всегда было куда нажимать
        try:
            await q.edit_message_text(
                _safe_markdown(reply),
                reply_markup=kb_match_hub(match_id),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            await q.message.reply_text(
                _safe_markdown(reply),
                reply_markup=kb_match_hub(match_id),
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    # fallback
    try:
        await q.edit_message_text("Не понял действие. Открой меню.", reply_markup=kb_main_menu())
    except Exception:
        await q.message.reply_text("Не понял действие. Открой меню.", reply_markup=kb_main_menu())


# ---------------------------------------------------------------------
# FastAPI webhook router
# ---------------------------------------------------------------------
def _ensure_app() -> Application:
    global _tg_app

    if _tg_app is not None:
        return _tg_app

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    _tg_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    _tg_app.add_handler(CallbackQueryHandler(handle_callback))
    _tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    _tg_app.add_handler(MessageHandler(filters.COMMAND, handle_message))

    return _tg_app


def mount_telegram_routes(fastapi_app: FastAPI) -> None:
    """
    Вызывается из src/service.py на старте:
    mount_telegram_routes(app)
    """
    global _router

    app = _ensure_app()

    router = APIRouter()

    @router.post(WEBHOOK_PATH)
    async def telegram_webhook(request: Request) -> Response:
        try:
            payload = await request.json()
        except Exception:
            return Response(status_code=400, content="bad json")

        try:
            update = Update.de_json(payload, app.bot)
            await app.process_update(update)
            return Response(status_code=200, content="ok")
        except Exception:
            logger.exception("Unhandled telegram error")
            return Response(status_code=200, content="ok")

    # (опционально) проверка живости
    @router.get("/telegram/health")
    async def telegram_health() -> Dict[str, Any]:
        return {"ok": True}

    fastapi_app.include_router(router)
    _router = router


async def telegram_startup() -> None:
    """
    Запускается из startup hook FastAPI.
    """
    app = _ensure_app()

    await app.initialize()
    await app.start()

    # ставим webhook, если есть PUBLIC_URL
    if WEBHOOK_URL:
        try:
            await app.bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            pass
        try:
            ok = await app.bot.set_webhook(url=WEBHOOK_URL)
            logger.info("Telegram webhook set: %s (ok=%s)", WEBHOOK_URL, ok)
        except Exception:
            logger.exception("Failed to set webhook")
    else:
        logger.warning("PUBLIC_URL is missing -> webhook not set")


async def telegram_shutdown() -> None:
    """
    Останавливаем PTB приложение корректно.
    """
    global _tg_app
    if _tg_app is None:
        return
    try:
        await _tg_app.stop()
    except Exception:
        pass
    try:
        await _tg_app.shutdown()
    except Exception:
        pass
    _tg_app = None
