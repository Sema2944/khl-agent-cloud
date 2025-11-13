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

logger = logging.getLogger(__name__)

# Глобальный объект приложения бота
_bot_app: Optional[Application] = None


# ====================== HANDLERS ======================

async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    # ВАЖНО: только обычный текст, без <b>, <i>, <текст> и т.п.
    text = (
        "Привет! Я бот учёта ставок.\n\n"
        "Доступные команды:\n"
        "/addbet Описание ставки — добавить ставку\n"
        "/clearbets — очистить список ставок\n"
        "/help — показать справку\n"
    )

    await update.message.reply_text(text)


async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    text = (
        "Справка по боту:\n\n"
        "/start — запустить бота и показать приветствие\n"
        "/addbet Описание ставки — добавить ставку\n"
        "/clearbets — удалить все сохранённые ставки\n"
    )

    await update.message.reply_text(text)


async def _cmd_addbet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    if not context.args:
        await update.message.reply_text("Использование: /addbet Описание ставки")
        return

    description = " ".join(context.args)
    await update.message.reply_text(f"Ставка добавлена: {description}")


async def _cmd_clearbets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    # Здесь позже можно будет вызвать очистку из БД
    await update.message.reply_text("Все ставки очищены (заглушка).")


async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "Я пока понимаю только команды.\n"
        "Напиши /help, чтобы посмотреть список доступных команд."
    )


# ====================== ВСПОМОГАТЕЛЬНОЕ ======================

def _create_application(token: str) -> Application:
    """
    Создаём Application и регистрируем хендлеры.
    НИКАКОГО parse_mode здесь нет.
    """
    app = ApplicationBuilder().token(token).build()

    # Команды
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("help", _cmd_help))
    app.add_handler(CommandHandler("addbet", _cmd_addbet))
    app.add_handler(CommandHandler("clearbets", _cmd_clearbets))

    # Любой текст без команды
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))

    return app


async def build_bot_app() -> Optional[Application]:
    """
    Создаёт и кэширует Application, если задан TELEGRAM_TOKEN.
    Ничего не запускает, только строит.
    """
    global _bot_app

    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.warning("⚠️ TELEGRAM_TOKEN не задан — бот не будет запущен.")
        return None

    if _bot_app is None:
        _bot_app = _create_application(token)
        logger.info("[BOT] Application создан.")

    return _bot_app


# ====================== ЗАПУСК / ОСТАНОВКА (ASGI-style) ======================

_bot_started: bool = False  # наш флаг, чтобы не стартовать несколько раз


async def start_bot_polling() -> None:
    """
    Стартуем бота внутри того же event loop, что и FastAPI/uvicorn.
    Без run_polling, без потоков — только initialize/start/updater.start_polling.
    """
    global _bot_app, _bot_started

    if _bot_app is None:
        logger.warning("[BOT] start_bot_polling вызван, но _bot_app is None.")
        return

    if _bot_started:
        logger.info("[BOT] Уже запущен, пропускаем повторный start.")
        return

    # Инициализация Application
    await _bot_app.initialize()
    await _bot_app.start()

    # На всякий случай чистим webhook и сбрасываем старые апдейты
    try:
        await _bot_app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logger.exception("[BOT] Не удалось удалить webhook: %s", e)

    # Запускаем polling через updater
    if _bot_app.updater is not None:
        await _bot_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("[BOT] Polling запущен.")
        _bot_started = True
    else:
        logger.warning("[BOT] У Application нет updater — polling не запущен.")


async def stop_bot_polling() -> None:
    """
    Корректно останавливает бота при остановке приложения.
    """
    global _bot_app, _bot_started

    if _bot_app is None or not _bot_started:
        logger.info("[BOT] stop_bot_polling: бот уже остановлен или не запускался.")
        return

    if _bot_app.updater is not None:
        await _bot_app.updater.stop()

    await _bot_app.stop()
    await _bot_app.shutdown()

    _bot_started = False
    logger.info("[BOT] Остановлен.")







