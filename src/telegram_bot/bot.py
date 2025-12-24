from __future__ import annotations

import os
import logging
import asyncio
import re

import httpx
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

# -------------------------------------------------
# CONFIG
# -------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
API_BASE = (os.getenv("API_BASE") or "").strip().rstrip("/")
BACKEND_TIMEOUT = float(os.getenv("BACKEND_TIMEOUT", "8"))

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

# -------------------------------------------------
# MAIN MENU (ОДНО)
# -------------------------------------------------
MAIN_KB = ReplyKeyboardMarkup(
    [
        ["🏟 Матчи сегодня"],
        ["🧠 AI Аналитика", "👤 Стратегия эксперта"],
    ],
    resize_keyboard=True,
)

# -------------------------------------------------
# INLINE KEYBOARDS
# -------------------------------------------------
def sports_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏒 Хоккей", callback_data="SPORT:hockey")],
            [InlineKeyboardButton("⚽ Футбол", callback_data="SPORT:football")],
            [InlineKeyboardButton("🏀 Баскетбол", callback_data="SPORT:basketball")],
            [InlineKeyboardButton("🎾 Теннис", callback_data="SPORT:tennis")],
            [InlineKeyboardButton("🕹 Киберспорт", callback_data="SPORT:esports")],
        ]
    )


def match_actions_keyboard(match_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📈 Линия", callback_data=f"MATCH_LINE:{match_id}"),
                InlineKeyboardButton("🧠 AI разбор", callback_data=f"MATCH_AI:{match_id}"),
            ],
            [
                InlineKeyboardButton(
                    "👤 Мнение эксперта",
                    callback_data=f"MATCH_EXPERT:{match_id}",
                )
            ],
        ]
    )


# -------------------------------------------------
# BACKEND HELPERS
# -------------------------------------------------
async def _safe_request(method: str, url: str, **kwargs) -> dict:
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        r = await client.request(method, url, **kwargs)
        r.raise_for_status()
        return r.json()


async def call_agent(user_id: int, text: str) -> str:
    payload = {"user_id": user_id, "message": text}
    data = await _safe_request("POST", f"{API_BASE}/agent/query", json=payload)
    return data.get("reply", "Пустой ответ от сервера")


# -------------------------------------------------
# COMMANDS
# -------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет 👋\n\nВыбери действие:",
        reply_markup=MAIN_KB,
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Бот жив", reply_markup=MAIN_KB)


# -------------------------------------------------
# MESSAGE HANDLER
# -------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.effective_user.id
    text = update.message.text.strip().lower()

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.TYPING,
    )

    # --- MAIN BUTTONS ---
    if "матчи сегодня" in text:
        await update.message.reply_text(
            "Выбери вид спорта:",
            reply_markup=sports_keyboard(),
        )
        return

    if "ai аналитика" in text:
        await update.message.reply_text(
            "Напиши:\n"
            "`аналитика <id матча>`\n"
            "или\n"
            "`аналитика <вопрос>`",
            reply_markup=MAIN_KB,
        )
        return

    if "стратегия эксперта" in text:
        reply = await call_agent(user_id, "стратегия")
        await update.message.reply_text(reply, reply_markup=MAIN_KB)
        return

    # --- EVERYTHING ELSE → BACKEND ---
    reply = await call_agent(user_id, update.message.text)
    await update.message.reply_text(reply, reply_markup=MAIN_KB)


# -------------------------------------------------
# CALLBACKS
# -------------------------------------------------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    # --- SPORT SELECT ---
    if data.startswith("SPORT:"):
        sport = data.split(":", 1)[1]
        reply = await call_agent(user_id, f"матчи сегодня {sport}")
        # пробуем найти id матча
        m = re.search(r"id:\s*([a-zA-Z0-9_\-:.]+)", reply)
        if m:
            await query.message.reply_text(
                reply,
                reply_markup=match_actions_keyboard(m.group(1)),
            )
        else:
            await query.message.reply_text(reply, reply_markup=MAIN_KB)
        return

    # --- MATCH ACTIONS ---
    if data.startswith("MATCH_LINE:"):
        match_id = data.split(":", 1)[1]
        reply = await call_agent(user_id, f"линия {match_id}")
        await query.message.reply_text(reply, reply_markup=MAIN_KB)
        return

    if data.startswith("MATCH_AI:"):
        match_id = data.split(":", 1)[1]
        reply = await call_agent(user_id, f"аналитика {match_id}")
        await query.message.reply_text(reply, reply_markup=MAIN_KB)
        return

    if data.startswith("MATCH_EXPERT:"):
        reply = await call_agent(user_id, "стратегия")
        await query.message.reply_text(reply, reply_markup=MAIN_KB)
        return


# -------------------------------------------------
# MAIN
# -------------------------------------------------
def main():
    logger.info("Telegram bot started")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling(stop_signals=None)


if __name__ == "__main__":
    main()
