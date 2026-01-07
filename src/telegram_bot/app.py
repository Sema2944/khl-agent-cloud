from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

from fastapi import FastAPI, Request
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from ..user_store import get_or_create_user

logger = logging.getLogger(__name__)

# -----------------------------
# ENV
# -----------------------------
BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").strip().rstrip("/")
WEBHOOK_PATH = (os.getenv("TELEGRAM_WEBHOOK_PATH") or "/telegram/webhook").strip()

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

# -----------------------------
# MAIN MENU (ReplyKeyboard)
# -----------------------------
MAIN_KB = ReplyKeyboardMarkup(
    [
        ["🏟 Матчи сегодня"],
        ["🧠 AI Аналитика", "👤 Стратегия эксперта"],
        ["📊 Профиль", "⭐ Premium"],
    ],
    resize_keyboard=True,
)

# -----------------------------
# SPORTS
# -----------------------------
SPORTS = [
    ("hockey", "🏒 Хоккей"),
    ("football", "⚽ Футбол"),
    ("basketball", "🏀 Баскетбол"),
    ("tennis", "🎾 Теннис"),
]

ID_RE = re.compile(r"id:\s*([a-zA-Z0-9_\-:.]{4,120})", re.IGNORECASE)

# -----------------------------
# KEYBOARDS
# -----------------------------
def kb_sports() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(label, callback_data=f"SPORT:{key}")] for key, label in SPORTS]
    rows.append([InlineKeyboardButton("⬅️ В меню", callback_data="BACK:MENU")])
    return InlineKeyboardMarkup(rows)


def kb_matches(matches: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(title, callback_data=f"MATCH:{match_id}")] for match_id, title in matches]
    rows.append([InlineKeyboardButton("⬅️ К видам спорта", callback_data="BACK:SPORTS")])
    return InlineKeyboardMarkup(rows)


def kb_match_hub(match_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Обзор рынков", callback_data=f"OVERVIEW:{match_id}")],
            [
                InlineKeyboardButton("🧠 1X2", callback_data=f"UI:{match_id}:pre:moneyline"),
                InlineKeyboardButton("🧠 Тотал", callback_data=f"UI:{match_id}:pre:total"),
            ],
            [InlineKeyboardButton("🧠 Фора", callback_data=f"UI:{match_id}:pre:handicap")],
            [
                InlineKeyboardButton("🟢 LIVE", callback_data=f"UI:{match_id}:live:overview"),
                InlineKeyboardButton("🔄 Обновить", callback_data=f"UI:{match_id}:live:refresh"),
            ],
            [InlineKeyboardButton("⬅️ К матчам", callback_data="BACK:MATCHES")],
        ]
    )


def kb_market_overview(match_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🧠 1X2", callback_data=f"UI:{match_id}:pre:moneyline")],
            [InlineKeyboardButton("🧠 Тотал", callback_data=f"UI:{match_id}:pre:total")],
            [InlineKeyboardButton("🧠 Фора", callback_data=f"UI:{match_id}:pre:handicap")],
            [InlineKeyboardButton("🟢 LIVE", callback_data=f"UI:{match_id}:live:overview")],
            [InlineKeyboardButton("⬅️ К матчу", callback_data="BACK:MATCH_HUB")],
        ]
    )


def kb_premium() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔓 Активировать Premium", callback_data="PAY:PREMIUM")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="BACK:MENU")],
        ]
    )

# -----------------------------
# TEXTS
# -----------------------------
MARKET_OVERVIEW_TEXT = """📊 Обзор рынков

Здесь — основные направления для анализа 👇

1️⃣ 1X2 (исход)
• Кто контролирует игру.
• Есть ли перекос линии.
• Часто даёт value в LIVE.

2️⃣ Тоталы
• Зависят от темпа.
• Особенно чувствительны по ходу матча.

3️⃣ Фора
• Работает при разнице в классе.
• Полезна при затяжном давлении.

4️⃣ LIVE
• Основная зона value.
• Линия меняется быстрее, чем игра.

Выбирай рынок для детального разбора 👇

Аналитический материал, не является рекомендацией.
"""

# -----------------------------
# HELPERS
# -----------------------------
async def call_agent_local(user_id: int, message: str) -> str:
    from ..parsing import run_dialog_agent
    return await run_dialog_agent(user_id=user_id, message=message)


async def edit_or_send(msg: Message, text: str, reply_markup=None):
    try:
        await msg.edit_text(text, reply_markup=reply_markup)
    except BadRequest:
        await msg.reply_text(text, reply_markup=reply_markup)


# -----------------------------
# ERROR HANDLER
# -----------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled telegram error: %s", context.error)


# -----------------------------
# HANDLERS
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    tg = update.effective_user

    # ✅ FIX: только tg_id позиционно, остальное keyword
    get_or_create_user(
        tg.id,
        username=tg.username,
        first_name=tg.first_name,
        last_name=tg.last_name,
    )

    await update.message.reply_text(
        "✅ Я на связи.\n\nВыбирай действие кнопками ниже 👇",
        reply_markup=MAIN_KB,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    tg = update.effective_user
    text = (update.message.text or "").strip()
    low = text.lower()

    # ✅ FIX: keyword args
    get_or_create_user(
        tg.id,
        username=tg.username,
        first_name=tg.first_name,
        last_name=tg.last_name,
    )

    if "матчи" in low:
        await update.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
        return

    if "premium" in low or "премиум" in low:
        await update.message.reply_text(
            "🔓 Premium (скоро)\n\nПока доступно вручную.",
            reply_markup=kb_premium(),
        )
        return

    reply = await call_agent_local(tg.id, text)
    await update.message.reply_text(reply, reply_markup=MAIN_KB)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.message:
        return

    data = q.data or ""
    await q.answer()

    # обновим user (на всякий)
    tg = q.from_user
    get_or_create_user(
        tg.id,
        username=tg.username,
        first_name=tg.first_name,
        last_name=tg.last_name,
    )

    if data == "BACK:MENU":
        await edit_or_send(q.message, "Выбирай действие 👇", reply_markup=MAIN_KB)
        return

    if data == "BACK:SPORTS":
        await edit_or_send(q.message, "🏟 Выбери спорт:", reply_markup=kb_sports())
        return

    if data == "BACK:MATCH_HUB":
        match_id = context.user_data.get("active_match")
        if not match_id:
            await edit_or_send(q.message, "🏟 Выбери спорт:", reply_markup=kb_sports())
            return
        await edit_or_send(q.message, "Выбери действие:", reply_markup=kb_match_hub(match_id))
        return

    if data.startswith("SPORT:"):
        sport = data.split(":", 1)[1].strip()
        text = await call_agent_local(tg.id, f"матчи сегодня {sport}")

        matches: list[tuple[str, str]] = []
        for line in text.splitlines():
            m = ID_RE.search(line)
            if not m:
                continue
            match_id = m.group(1).strip()
            title = line.strip()
            matches.append((match_id, title))

        context.user_data["last_matches"] = matches
        context.user_data["last_sport"] = sport

        if matches:
            await edit_or_send(q.message, text, reply_markup=kb_matches(matches))
        else:
            await edit_or_send(q.message, text, reply_markup=kb_sports())
        return

    if data.startswith("MATCH:"):
        match_id = data.split(":", 1)[1].strip()
        context.user_data["active_match"] = match_id

        reply = await call_agent_local(tg.id, f"матч {match_id}")
        await edit_or_send(q.message, reply, reply_markup=kb_match_hub(match_id))
        return

    if data.startswith("OVERVIEW:"):
        match_id = data.split(":", 1)[1].strip()
        context.user_data["active_match"] = match_id
        await edit_or_send(q.message, MARKET_OVERVIEW_TEXT, reply_markup=kb_market_overview(match_id))
        return

    if data.startswith("UI:"):
        # Приводим к формату, который ждёт агент: "ui match <id> <mode> <action>"
        parts = data.split(":")
        if len(parts) == 4:
            _, match_id, mode, action = parts
            context.user_data["active_match"] = match_id
            reply = await call_agent_local(tg.id, f"ui match {match_id} {mode} {action}")
        else:
            reply = "Некорректная команда UI."

        await edit_or_send(q.message, reply, reply_markup=kb_match_hub(context.user_data.get("active_match", "unknown")))
        return

    if data == "PAY:PREMIUM":
        await edit_or_send(
            q.message,
            "🔓 Premium (скоро)\n\n"
            "Архитектура подписок уже готова.\n"
            "Следующий шаг — подключить оплату (Telegram Payments / YooKassa) и вебхук.\n\n"
            "Пока Premium можно активировать вручную через админ-команду (добавим).",
            reply_markup=kb_premium(),
        )
        return

    await edit_or_send(q.message, "Не понял действие 🤔", reply_markup=MAIN_KB)


# -----------------------------
# APP FACTORY + FASTAPI MOUNT
# -----------------------------
def build_telegram_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)  # ✅ FIX: чтобы не было "No error handlers"
    return app


def mount(fastapi_app: FastAPI):
    tg_app = build_telegram_application()
    fastapi_app.state.telegram = tg_app

    async def startup():
        await tg_app.initialize()
        await tg_app.start()
        if PUBLIC_URL:
            await tg_app.bot.set_webhook(f"{PUBLIC_URL}{WEBHOOK_PATH}")
        else:
            logger.warning("PUBLIC_URL is not set -> webhook not configured automatically")

    async def shutdown():
        try:
            await tg_app.stop()
        finally:
            await tg_app.shutdown()

    fastapi_app.add_event_handler("startup", startup)
    fastapi_app.add_event_handler("shutdown", shutdown)

    @fastapi_app.post(WEBHOOK_PATH)
    async def webhook(req: Request) -> dict[str, Any]:
        payload = await req.json()
        update = Update.de_json(payload, tg_app.bot)
        await tg_app.process_update(update)
        return {"ok": True}
