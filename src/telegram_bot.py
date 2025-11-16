# src/telegram_bot.py

import os
import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import httpx

API_BASE = os.getenv("API_BASE", "https://khl-agent-api.onrender.com")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def call_agent(user_id: int, message: str) -> str:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{API_BASE}/agent/query",
            json={"user_id": user_id, "message": message},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("reply", "Пустой ответ от агента")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я AI-агент для ставок на спорт.\n"
        "Напиши, например:\n"
        "— 'Покажи мою статистику'\n"
        "— 'Какие матчи КХЛ сегодня?'"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    user_id = update.effective_user.id
    text = update.message.text or ""

    try:
        reply = await call_agent(user_id, text)
    except Exception:
        logger.exception("Ошибка при вызове агента")
        reply = "Не смог связаться с сервером агента, попробуй позже."

    await update.message.reply_text(reply)


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в переменных окружения")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()


if __name__ == "__main__":
    main()
