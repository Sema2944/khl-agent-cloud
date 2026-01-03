# src/telegram_bot/bot.py
from __future__ import annotations

import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

"""
ВАЖНО:
Этот модуль оставлен ТОЛЬКО для локальной отладки (polling).
В проде на Render мы используем webhook-бота из src/telegram_bot/app.py
и НЕ должны запускать polling вторым сервисом/воркером.

Если случайно запустить этот файл на Render — он сразу упадёт с понятной ошибкой.
"""

def main() -> None:
    env = (os.getenv("ENV") or "").strip().lower()
    on_render = bool(os.getenv("RENDER")) or bool(os.getenv("RENDER_SERVICE_ID"))

    if on_render or env in {"prod", "production"}:
        raise RuntimeError(
            "Polling bot is disabled in production. "
            "Use webhook bot in src/telegram_bot/app.py and run only the web service."
        )

    # Локальная отладка polling (если реально нужно)
    from telegram.ext import Application, CommandHandler
    from telegram import Update
    from telegram.ext import ContextTypes

    bot_token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message:
            await update.message.reply_text("✅ Polling mode (local debug).")

    app = Application.builder().token(bot_token).build()
    app.add_handler(CommandHandler("start", start))

    logger.info("Starting polling bot (LOCAL ONLY).")
    app.run_polling(stop_signals=None)


if __name__ == "__main__":
    main()
