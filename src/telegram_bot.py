import asyncio
import logging
import os
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

log = logging.getLogger(__name__)

# Глобальные ссылки, чтобы FastAPI мог корректно останавливать бота
_bot_app: Optional[Application] = None


# ==========================
#      ХЕНДЛЕРЫ КОМАНД
# ==========================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ответ на /start"""
    log.info("[BOT] Обработана команда /start от %s", update.effective_user.id)

    text = (
        "Привет! Я бот KHL Agent.\n\n"
        "Доступные команды:\n"
        "/start — это сообщение\n"
        "/help — помощь\n"
        "/health — проверка статуса бота\n"
    )
    # ВАЖНО: без HTML/Markdown, чтобы не ловить BadRequest
    await update.message.reply_text(text)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("[BOT] Обработана команда /help от %s", update.effective_user.id)
    text = (
        "Помощь по боту:\n\n"
        "Пока доступен минимальный набор команд:\n"
        "/start — приветствие\n"
        "/help — это сообщение\n"
        "/health — проверка статуса\n"
    )
    await update.message.reply_text(text)


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("[BOT] Обработана команда /health от %s", update.effective_user.id)
    await update.message.reply_text("✅ Бот запущен и работает.")


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ответ на незнакомые команды"""
    if update.message and update.message.text and update.message.text.startswith("/"):
        log.info("[BOT] Неизвестная команда: %s", update.message.text)
        await update.message.reply_text("Я пока не знаю такую команду 🙈\nПопробуй /help.")


# ==========================
#   ЛОГИРОВАНИЕ АПДЕЙТОВ
# ==========================

async def log_any_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Хендлер для логирования любых апдейтов (для отладки)."""
    log.info("[BOT] Получен апдейт: %s", update)


# ==========================
#   СБОРКА ПРИЛОЖЕНИЯ БОТА
# ==========================

async def build_bot_app() -> Optional[Application]:
    """
    Создаёт и настраивает Application для бота.
    Вызывается из FastAPI при старте сервиса.
    """
    global _bot_app

    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        log.warning("⚠️ TELEGRAM_TOKEN не задан — бот не будет запущен.")
        return None

    log.info("[BOT] Инициализация Application...")

    app = (
        ApplicationBuilder()
        .token(token)
        # НИКАКОГО parse_mode, чтобы не словить BadRequest на разметке
        .build()
    )

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("health", cmd_health))

    # Неизвестные команды (сообщения, начинающиеся с /)
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown), group=0)

    # Логирование любых апдейтов для диагностики
    app.add_handler(MessageHandler(filters.ALL, log_any_update), group=1)

    _bot_app = app
    log.info("[BOT] Application создан.")
    return app


# ==========================
#    ЗАПУСК / СТОП ПОЛЛИНГА
# ==========================

async def run_polling(app: Application) -> None:
    """
    Запуск long polling в фоне.
    Вызывается из FastAPI on_startup через asyncio.create_task(run_polling(...)).
    """
    log.info("[BOT] Запуск polling...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    log.info("[BOT] Polling запущен.")

    # Ждём, пока приложение не будет остановлено
    # (updater.start_polling() сам держит цикл до stop())
    await app.updater.wait_for_stop()

    log.info("[BOT] Polling завершён.")


async def stop_polling(app: Application) -> None:
    """
    Корректная остановка polling.
    Вызывается из FastAPI on_shutdown.
    """
    log.info("[BOT] Остановка polling...")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    log.info("[BOT] Бот остановлен.")







