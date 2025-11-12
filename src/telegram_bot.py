import os
import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

logger = logging.getLogger(__name__)

# ====== Команды ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я бот KHL Agent Cloud.")

async def add_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("⚠️ Введите текст ставки после команды /addbet.")
        return
    await update.message.reply_text(f"✅ Ставка добавлена: {text}")

async def bets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🧾 Пока ставок нет (демо-режим).")

async def clear_bets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Команда недоступна в демо-режиме.")


# ====== Инициализация ======
async def build_bot_app():
    token = os.getenv("TELEGRAM_TOKEN")

    if not token:
        logger.warning("⚠️ TELEGRAM_TOKEN не задан — бот не будет запущен.")
        return None  # не ломаем FastAPI, просто пропускаем бота

    try:
        application = (
            ApplicationBuilder()
            .token(token)
            .build()
        )

        # Регистрируем команды
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("addbet", add_bet))
        application.add_handler(CommandHandler("bets", bets))
        application.add_handler(CommandHandler("clearbets", clear_bets))

        logger.info("[BOT] Приложение Telegram собрано успешно.")
        return application
    except Exception as e:
        logger.exception(f"[BOT] Ошибка при инициализации бота: {e}")
        return None





