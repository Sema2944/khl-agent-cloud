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


# URL твоего бекенда-агента (FastAPI)
API_BASE = os.getenv("API_BASE", "https://khl-agent-api.onrender.com")

# Токен бота из BotFather
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def call_agent(user_id: int, message: str) -> str:
    """
    Шлёт запрос в твой /agent/query и возвращает текст ответа.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{API_BASE}/agent/query",
            json={"user_id": user_id, "message": message},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("reply", "Пустой ответ от агента 😕")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start — приветственное сообщение.
    """
    await update.message.reply_text(
        "Привет! Я AI-агент для ставок на спорт.\n\n"
        "Я умею:\n"
        "• Показать твою статистику по ставкам (напиши: 'Покажи мою статистику')\n"
        "• Показать матчи КХЛ на сегодня (напиши: 'Какие матчи КХЛ сегодня?')\n\n"
        "Напиши мне что-нибудь 😉"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатываем любое текстовое сообщение:
    → отправляем его на бекенд-агент
    → возвращаем ответ пользователю.
    """
    if not update.message:
        return

    user_id = update.effective_user.id
    text = update.message.text or ""

    try:
        reply = await call_agent(user_id, text)
    except Exception as e:
        logger.exception("Ошибка при вызове агента: %s", e)
        reply = (
            "Не удалось связаться с сервером агента 😔\n"
            "Попробуй ещё раз чуть позже."
        )

    await update.message.reply_text(reply)


def main() -> None:
    """
    Точка входа: запускает Telegram-бота в режиме polling.
    """
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в переменных окружения")

    logger.info("Запускаю Telegram-бота...")

    app = Application.builder().token(BOT_TOKEN).build()

    # Команда /start
    app.add_handler(CommandHandler("start", start))
    # Все текстовые сообщения (кроме команд) — в агент
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()


if __name__ == "__main__":
    main()
