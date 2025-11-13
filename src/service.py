import os
import logging

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

logger = logging.getLogger(__name__)


# ==== Команды бота ====


async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ответ на /start."""
    text = (
        "Привет! 👋\n\n"
        "Я бот для трекинга ставок.\n\n"
        "Доступные команды:\n"
        "  /start – показать это сообщение\n"
        "  /help – помощь\n"
        "  /addbet <текст> – добавить ставку\n"
        "  /clearbets – очистить ставки\n"
    )
    await update.message.reply_text(text)
    logger.info("Handled /start from chat_id=%s", update.effective_chat.id)


async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Помощь по боту:\n\n"
        "/start – приветствие и список команд\n"
        "/addbet <текст> – сохранить новую ставку\n"
        "/clearbets – удалить все сохранённые ставки\n"
    )
    await update.message.reply_text(text)
    logger.info("Handled /help from chat_id=%s", update.effective_chat.id)


async def _cmd_addbet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Тупо эхо, чтобы проверить, что команда работает."""
    args_text = " ".join(context.args) if context.args else "(пусто)"
    text = f"Добавлена ставка: {args_text}"
    await update.message.reply_text(text)
    logger.info(
        "Handled /addbet from chat_id=%s, text=%r",
        update.effective_chat.id,
        args_text,
    )


async def _cmd_clearbets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Тут потом можно подвязать БД. Пока просто заглушка.
    await update.message.reply_text("Все ставки очищены (пока это просто заглушка).")
    logger.info("Handled /clearbets from chat_id=%s", update.effective_chat.id)


# ==== Построение Application ====


def build_bot_app() -> Application | None:
    """Создаёт и настраивает Telegram Application.

    Если TELEGRAM_TOKEN не задан — возвращает None.
    """
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.warning("⚠️ TELEGRAM_TOKEN не задан — бот не будет запущен.")
        return None

    app = ApplicationBuilder().token(token).build()

    # Регистрируем хэндлеры
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("help", _cmd_help))
    app.add_handler(CommandHandler("addbet", _cmd_addbet))
    app.add_handler(CommandHandler("clearbets", _cmd_clearbets))

    logger.info("✅ Telegram Application создан.")
    return app


# ==== Запуск / остановка в рамках FastAPI ====

async def start_bot_polling(app: Application) -> None:
    """Запускает бота в режиме long polling (НЕ блокируя FastAPI).

    Важно: НЕ используем app.run_polling(), потому что он сам управляет loop.
    Тут — "ручной" запуск для интеграции с FastAPI.
    """
    if app is None:
        logger.warning("start_bot_polling вызван с app=None, выходим.")
        return

    logger.info("[BOT] Инициализация Application...")
    await app.initialize()
    await app.start()

    # Сбрасываем старые непрочитанные обновления, чтобы не обрабатывать древние /start
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("[BOT] Polling запущен.")


async def stop_bot_polling(app: Application) -> None:
    """Корректная остановка бота при выключении сервиса."""
    if app is None:
        return

    logger.info("[BOT] Остановка polling...")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    logger.info("[BOT] Остановлен.")



