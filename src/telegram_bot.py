import os
import logging
from typing import Optional

from telegram import Update, BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ===================== Команды =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я бот KHL Agent Cloud. Команды: /addbet, /bets, /clearbets")

async def add_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("⚠️ Использование: /addbet <текст ставки>")
        return
    # тут могла бы быть запись в БД
    await update.message.reply_text(f"✅ Ставка добавлена: {text}")

async def bets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # демо-ответ
    await update.message.reply_text("🧾 Пока ставок нет (демо).")

async def clear_bets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Команда недоступна в демо-режиме.")

# ===================== Инициализация =====================

async def build_bot_app() -> Optional[Application]:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.warning("⚠️ TELEGRAM_TOKEN не задан — бот не будет запущен.")
        return None

    # Собираем Application
    app = (
        ApplicationBuilder()
        .token(token)
        .parse_mode(ParseMode.HTML)            # PTB v21: метод билдера .parse_mode(...)
        .concurrent_updates(True)              # безопасная обработка апдейтов параллельно
        .build()
    )

    # Регистрируем команды/хэндлеры
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addbet", add_bet))
    app.add_handler(CommandHandler("bets", bets))
    app.add_handler(CommandHandler("clearbets", clear_bets))

    # Команды для меню Telegram
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Запустить бота"),
            BotCommand("addbet", "Добавить ставку"),
            BotCommand("bets", "Список ставок"),
            BotCommand("clearbets", "Очистить ставки (демо)"),
        ])
    except Exception:
        logger.exception("[BOT] Не удалось установить команды меню")

    logger.info("[BOT] Приложение Telegram собрано.")
    return app

# ===================== Запуск/остановка polling =====================

async def run_polling(app: Application):
    """
    Полный корректный цикл для polling в PTB v21:
      - удалить webhook (если был)
      - initialize/start
      - start_polling (через app.updater)
    """
    # На всякий случай убираем webhook
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        logger.exception("[BOT] Не удалось удалить webhook")

    # initialize() -> start() -> start polling
    await app.initialize()
    await app.start()

    # В PTB v21 polling запускается через updater.start_polling()
    # Он работает в фоне, метод возвращается сразу.
    await app.updater.start_polling()
    logger.info("[BOT] Updater polling запущен.")

    # Ничего не ждём здесь: polling работает в фоновых тасках PTB.
    # Функция завершается, FastAPI продолжает жить.

async def stop_polling(app: Application):
    """Аккуратная остановка бота."""
    try:
        await app.updater.stop()
    except Exception:
        logger.exception("[BOT] Ошибка при остановке updater")

    try:
        await app.stop()
    except Exception:
        logger.exception("[BOT] Ошибка при остановке app")

    try:
        await app.shutdown()
    except Exception:
        logger.exception("[BOT] Ошибка при shutdown app")

    logger.info("[BOT] Polling остановлен и приложение закрыто.")






