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
    Message,
)
from telegram.constants import ChatAction
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
from ..entitlements import get_effective_entitlements

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
    rows = [
        [InlineKeyboardButton(label, callback_data=f"SPORT:{key}")]
        for key, label in SPORTS
    ]
    rows.append([InlineKeyboardButton("⬅️ В меню", callback_data="BACK:MENU")])
    return InlineKeyboardMarkup(rows)


def kb_matches(matches: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(title, callback_data=f"MATCH:{match_id}")]
        for match_id, title in matches
    ]
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
            [InlineKeyboardButton("⬅️ К матчу", callback_data=f"BACK:MATCH_HUB")],
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
# HANDLERS
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    get_or_create_user(tg.id, tg.username, tg.first_name, tg.last_name)

    await update.message.reply_text(
        "✅ Я на связи.\n\nВыбирай действие кнопками ниже 👇",
        reply_markup=MAIN_KB,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    text = (update.message.text or "").lower()

    get_or_create_user(tg.id, tg.username, tg.first_name, tg.last_name)

    if "матчи" in text:
        await update.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
        return

    if "premium" in text:
        await update.message.reply_text(
            "🔓 Premium (скоро)\n\nПока доступно вручную.",
            reply_markup=kb_premium(),
        )
        return

    reply = await call_agent_local(tg.id, text)
    await update.message.reply_text(reply, reply_markup=MAIN_KB)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    await q.answer()

    if data == "BACK:MENU":
        await edit_or_send(q.message, "Выбирай действие 👇", reply_markup=MAIN_KB)
        return

    if data == "BACK:SPORTS":
        await edit_or_send(q.message, "🏟 Выбери спорт:", reply_markup=kb_sports())
        return

    if data == "BACK:MATCH_HUB":
        match_id = context.user_data.get("active_match")
        await edit_or_send(q.message, "Выбери действие:", reply_markup=kb_match_hub(match_id))
        return

    if data.startswith("SPORT:"):
        sport = data.split(":")[1]
        text = await call_agent_local(q.from_user.id, f"матчи сегодня {sport}")
        matches = [(m.group(1), line) for line in text.splitlines() if (m := ID_RE.search(line))]
        context.user_data["last_matches"] = matches
        await edit_or_send(q.message, text, reply_markup=kb_matches(matches))
        return

    if data.startswith("MATCH:"):
        match_id = data.split(":")[1]
        context.user_data["active_match"] = match_id
        reply = await call_agent_local(q.from_user.id, f"матч {match_id}")
        await edit_or_send(q.message, reply, reply_markup=kb_match_hub(match_id))
        return

    if data.startswith("OVERVIEW:"):
        match_id = data.split(":")[1]
        await edit_or_send(
            q.message,
            MARKET_OVERVIEW_TEXT,
            reply_markup=kb_market_overview(match_id),
        )
        return

    if data.startswith("UI:"):
        reply = await call_agent_local(q.from_user.id, data.replace("UI:", "ui "))
        await edit_or_send(q.message, reply)
        return


# -----------------------------
# APP FACTORY
# -----------------------------
def build_telegram_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app


def mount(fastapi_app: FastAPI):
    tg_app = build_telegram_application()
    fastapi_app.state.telegram = tg_app

    async def startup():
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.bot.set_webhook(f"{PUBLIC_URL}{WEBHOOK_PATH}")

    async def shutdown():
        await tg_app.stop()
        await tg_app.shutdown()

    fastapi_app.add_event_handler("startup", startup)
    fastapi_app.add_event_handler("shutdown", shutdown)

    @fastapi_app.post(WEBHOOK_PATH)
    async def webhook(req: Request):
        payload = await req.json()
        update = Update.de_json(payload, tg_app.bot)
        await tg_app.process_update(update)
        return {"ok": True}
