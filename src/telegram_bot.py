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

# Глобальный инстанс приложения бота
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
    # ЖЁСТКО запрещаем Telegram парсить HTML/Markdown
    await update.message.reply_text(text, parse_mode=None)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("[BOT] Обработана команда /help от %s", update.effective_user.id)
    text = (
        "Помощь по боту:\n\n"
        "Пока доступен минимальный набор команд:\n"
        "/start — приветствие\n"
        "/help — это сообщение\n"
        "/health — проверка статуса\n"
    )
    await update.message.reply_text(text, parse_mode=None)


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("[BOT] Обработана команда /health от %s", update.effective_user.id)
    await update.message.reply_text("✅ Бот запущен и работает.", parse_mode=None)


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ответ на неизвестные команды"""
    if update.message and update.message.text and update.message.text.startswith("/"):
        log.info("[BOT] Неизвестная команда: %s", update.message.text)
        await update.message.reply_text(
            "Я пока не знаю такую команду 🙈\nПопробуй /help.",
            parse_mode=None,
        )


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
        # parse_mode НЕ задаём, и в reply_text явно пишем parse_mode=None
        .build()
    )

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("health", cmd_health))

    # Неизвестные команды
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown), group=0)

    # Логирование всех апдейтов
    app.add_handler(MessageHandler(filters.ALL, log_any_update), group=1)

    _bot_app = app
    log.info("[BOT] Application создан.")
    return app


# ==========================
#    СТАРТ / СТОП БОТА
# ==========================

async def start_bot() -> None:
    """
    Инициализация и запуск polling.
    Этот метод вызывается из FastAPI on_startup.
    """
    global _bot_app

    if _bot_app is None:
        await build_bot_app()

    if _bot_app is None:
        # Нет токена — бота не запускаем
        log.warning("[BOT] Не удалось запустить бота — нет TELEGRAM_TOKEN.")
        return

    log.info("[BOT] Запуск polling...")

    # ВАЖНО: это всё асинхронные методы — они НЕ блокируют event loop навсегда
    await _bot_app.initialize()
    await _bot_app.start()
    await _bot_app.updater.start_polling()

    log.info("[BOT] Polling запущен.")


async def stop_bot() -> None:
    """
    Корректная остановка polling.
    Этот метод вызывается из FastAPI on_shutdown.
    """
    global _bot_app

    if _bot_app is None:
        return

    log.info("[BOT] Остановка polling...")
    await _bot_app.updater.stop()
    await _bot_app.stop()
    await _bot_app.shutdown()
    log.info("[BOT] Бот остановлен.")







