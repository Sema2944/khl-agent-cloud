# src/telegram_bot/app.py
from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

from fastapi import FastAPI, Request, HTTPException
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logger = logging.getLogger(__name__)

# -----------------------------
# ENV
# -----------------------------
BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").strip().rstrip("/")
WEBHOOK_PATH = (os.getenv("TELEGRAM_WEBHOOK_PATH") or "/telegram/webhook").strip()
WEBHOOK_SECRET = (os.getenv("TELEGRAM_WEBHOOK_SECRET") or "").strip()  # optional
# Если хочешь отдельный base path для кнопок/меню — пока не надо.

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

# -----------------------------
# Главное меню (минимально)
# -----------------------------
MAIN_KB = ReplyKeyboardMarkup(
    [
        ["🏟 Матчи сегодня"],
        ["🧠 AI Аналитика", "👤 Стратегия эксперта"],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)

# -----------------------------
# Inline клавиатуры (FSM через callback_data)
# -----------------------------
SPORTS = [
    ("hockey", "🏒 Хоккей"),
    ("football", "⚽ Футбол"),
    ("basketball", "🏀 Баскетбол"),
    ("tennis", "🎾 Теннис"),
    ("esports", "🎮 Киберспорт"),
]

MARKETS = [
    ("moneyline", "1X2 / Moneyline"),
    ("total", "Тотал"),
    ("handicap", "Фора"),
]


def kb_sports() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    buf: list[InlineKeyboardButton] = []
    for key, label in SPORTS:
        buf.append(InlineKeyboardButton(label, callback_data=f"SPORT:{key}"))
        if len(buf) == 2:
            rows.append(buf)
            buf = []
    if buf:
        rows.append(buf)
    return InlineKeyboardMarkup(rows)


def kb_matches(match_buttons: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(title, callback_data=f"MATCH:{match_id}")]
        for match_id, title in match_buttons
    ]
    rows.append([InlineKeyboardButton("⬅️ Назад к спорту", callback_data="BACK:SPORTS")])
    return InlineKeyboardMarkup(rows)


def kb_markets(match_id: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(label, callback_data=f"MARKET:{match_id}:{key}")]
        for key, label in MARKETS
    ]
    rows.append([InlineKeyboardButton("⬅️ Назад к спорту", callback_data="BACK:SPORTS")])
    return InlineKeyboardMarkup(rows)


def kb_market_actions(match_id: str, market_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📈 Рынок",
                    callback_data=f"SHOW_MARKET:{match_id}:{market_key}",
                ),
                InlineKeyboardButton(
                    "🧠 AI разбор",
                    callback_data=f"AI:{match_id}:{market_key}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "👤 Мнение эксперта (если есть на сегодня)",
                    callback_data="EXPERT_TODAY",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Назад к рынкам",
                    callback_data=f"BACK:MARKETS:{match_id}",
                )
            ],
        ]
    )


# -----------------------------
# Парсинг матчей из текста backend
# Ожидаем:
# • СКА — ЦСКА (КХЛ) — id: `demo_hockey_001`
# -----------------------------
ID_RE = re.compile(r"id:\s*`?([a-zA-Z0-9_\-:.]{4,120})`?", re.IGNORECASE)


def extract_match_buttons(text: str) -> list[tuple[str, str]]:
    buttons: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        m = ID_RE.search(line)
        if not m:
            continue
        match_id = m.group(1).strip()
        title = re.sub(r"\s*—\s*id:\s*`?.+`?\s*$", "", line).strip()
        title = title.lstrip("•").strip()
        if match_id and title:
            buttons.append((match_id, title))
    return buttons


def _norm_menu(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[^\w\sа-яё-]", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# -----------------------------
# Локальный вызов агента (без HTTP, быстрее и надёжнее)
# -----------------------------
async def call_agent_local(user_id: int, message: str) -> str:
    from ..parsing import run_dialog_agent  # локальный импорт, чтобы не ломать startup
    return await run_dialog_agent(user_id=user_id, message=message)


# -----------------------------
# Handlers
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "✅ Я на связи.\n\nВыбирай действие кнопками ниже 👇",
        reply_markup=MAIN_KB,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    user_id = update.effective_user.id
    text = update.message.text or ""
    norm = _norm_menu(text)

    logger.info("tg.handle_message user_id=%s text=%r", user_id, text)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # --- Главное меню: Матчи сегодня ---
    if norm in {"матчи сегодня"}:
        await update.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
        # закрепим main menu отдельным сообщением
        await update.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # --- Главное меню: Стратегия эксперта ---
    if norm in {"стратегия эксперта", "стратегия", "эксперт", "эксперт сегодня"}:
        reply = await call_agent_local(user_id, "стратегия")
        await update.message.reply_text(reply, reply_markup=MAIN_KB, parse_mode="Markdown")
        return

    # --- Главное меню: AI аналитика (подсказка) ---
    if norm in {"ai аналитика", "аналитика", "ии аналитика"}:
        await update.message.reply_text(
            "Как пользоваться AI:\n"
            "1) нажми *🏟 Матчи сегодня*\n"
            "2) выбери спорт → матч → рынок\n"
            "3) нажми *🧠 AI разбор*\n\n"
            "Либо текстом: `аналитика <match_id> <market_key>`",
            reply_markup=MAIN_KB,
            parse_mode="Markdown",
        )
        return

    # --- Остальное: проброс в агента (ручные команды) ---
    reply = await call_agent_local(user_id, text)
    await update.message.reply_text(reply, reply_markup=MAIN_KB, parse_mode="Markdown")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    user_id = query.from_user.id
    await query.answer()

    logger.info("tg.callback user_id=%s data=%r", user_id, data)

    # BACK
    if data == "BACK:SPORTS":
        await query.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    if data.startswith("BACK:MARKETS:"):
        match_id = data.split(":", 2)[2]
        await query.message.reply_text("Выбери рынок:", reply_markup=kb_markets(match_id))
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # SPORT -> список матчей
    if data.startswith("SPORT:"):
        sport_key = data.split(":", 1)[1]
        reply = await call_agent_local(user_id, f"матчи сегодня {sport_key}")

        match_buttons = extract_match_buttons(reply)
        if not match_buttons:
            await query.message.reply_text(reply, parse_mode="Markdown")
            await query.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
            await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
            return

        await query.message.reply_text(reply, parse_mode="Markdown")
        await query.message.reply_text("Выбери матч:", reply_markup=kb_matches(match_buttons))
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # MATCH -> экран матча + рынки
    if data.startswith("MATCH:"):
        match_id = data.split(":", 1)[1]
        reply = await call_agent_local(user_id, f"матч {match_id}")

        await query.message.reply_text(reply, parse_mode="Markdown")
        await query.message.reply_text("Выбери рынок:", reply_markup=kb_markets(match_id))
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # MARKET -> действия
    if data.startswith("MARKET:"):
        _, match_id, market_key = data.split(":", 2)
        await query.message.reply_text(
            f"Выбран рынок: *{market_key}*\nВыбери действие:",
            reply_markup=kb_market_actions(match_id, market_key),
            parse_mode="Markdown",
        )
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # SHOW_MARKET
    if data.startswith("SHOW_MARKET:"):
        _, match_id, market_key = data.split(":", 2)
        reply = await call_agent_local(user_id, f"рынок {match_id} {market_key}")
        await query.message.reply_text(reply, parse_mode="Markdown")
        await query.message.reply_text("Действия:", reply_markup=kb_market_actions(match_id, market_key))
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # AI
    if data.startswith("AI:"):
        _, match_id, market_key = data.split(":", 2)
        reply = await call_agent_local(user_id, f"аналитика {match_id} {market_key}")
        await query.message.reply_text(reply, parse_mode="Markdown")
        await query.message.reply_text("Действия:", reply_markup=kb_market_actions(match_id, market_key))
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # EXPERT_TODAY
    if data == "EXPERT_TODAY":
        reply = await call_agent_local(user_id, "стратегия")
        await query.message.reply_text(reply, parse_mode="Markdown")
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    await query.message.reply_text("Не понял действие 🤔", reply_markup=MAIN_KB)


# -----------------------------
# Application factory (имя, которое ждёт service.py)
# -----------------------------
def build_telegram_application() -> Application:
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CallbackQueryHandler(handle_callback))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return tg_app


# -----------------------------
# FastAPI mount (webhook-only)
# -----------------------------
def mount(fastapi_app: FastAPI) -> None:
    """
    Регистрирует /telegram/webhook и lifecycle-хуки.
    НИКАКОГО polling.
    """
    tg_app = build_telegram_application()
    fastapi_app.state.telegram_app = tg_app

    async def _startup() -> None:
        # init/start PTB application
        await tg_app.initialize()
        await tg_app.start()

        if not PUBLIC_URL:
            logger.warning("PUBLIC_URL is not set -> webhook will NOT be configured automatically.")
            return

        webhook_url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
        try:
            await tg_app.bot.set_webhook(
                url=webhook_url,
                secret_token=WEBHOOK_SECRET or None,
                drop_pending_updates=True,
            )
            logger.info("Telegram webhook set: %s", webhook_url)
        except Exception as e:
            logger.exception("Failed to set telegram webhook: %s", e)

    async def _shutdown() -> None:
        try:
            await tg_app.stop()
        finally:
            await tg_app.shutdown()

    fastapi_app.add_event_handler("startup", _startup)
    fastapi_app.add_event_handler("shutdown", _shutdown)

    @fastapi_app.post(WEBHOOK_PATH)
    async def telegram_webhook(request: Request) -> dict[str, Any]:
        # (опционально) проверка secret token от Telegram
        if WEBHOOK_SECRET:
            got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if got != WEBHOOK_SECRET:
                raise HTTPException(status_code=403, detail="Bad webhook secret")

        payload = await request.json()
        update = Update.de_json(payload, tg_app.bot)
        await tg_app.process_update(update)
        return {"ok": True}
