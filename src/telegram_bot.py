import logging
import os
import threading

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logger = logging.getLogger(__name__)

# Глобальные объекты бота
_bot_app: Application | None = None
_bot_thread: threading.Thread | None = None


# ====================== HANDLERS ======================

async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    # ВАЖНО: НИКАКОЙ разметки <текст>, <b> и т.п. — только обычный текст
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

    # Минимальный функционал: просто подтверждаем приём ставки
    # (сюда позже можно подвезти твою реальную логику с БД)
    if not context.args:
        await update.message.reply_text("Использование: /addbet Описание ставки")
        return

    description = " ".join(context.args)
    await update.message.reply_text(f"Ставка добавлена: {description}")


async def _cmd_clearbets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    # Здесь можно будет вызвать очистку из БД, пока просто сообщение
    await update.message.reply_text("Все ставки очищены (заглушка).")


async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    # Простой echo-ответ на любое текстовое сообщение
    await update.message.reply_text(
        "Я пока понимаю только команды.\n"
        "Напиши /help, чтобы посмотреть список доступных команд."
    )


# ====================== ИНИЦИАЛИЗАЦИЯ БОТА ======================

def _build_application(token: str) -> Application:
    """
    Создаём объект Application и регистрируем хендлеры.
    Без parse_mode, чтобы не ловить ошибки парсинга сущностей.
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


async def build_bot_app() -> Application | None:
    """
    Строит Application, если задан TELEGRAM_TOKEN.
    Никакого run_polling здесь не вызываем.
    """
    global _bot_app

    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.warning("⚠️ TELEGRAM_TOKEN не задан — бот не будет запущен.")
        return None

    if _bot_app is None:
        _bot_app = _build_application(token)
        logger.info("[BOT] Application создан.")

    return _bot_app


# ====================== ЗАПУСК В ОТДЕЛЬНОМ ПОТОКЕ ======================

def start_bot_polling_in_thread() -> None:
    """
    Запускает run_polling в отдельном потоке, чтобы не конфликтовать
    с event loop uvicorn'а. Поэтому НЕТ ошибки
    'RuntimeError: this event loop is already running'.
    """
    global _bot_app, _bot_thread

    if _bot_app is None:
        logger.warning("[BOT] Нечего запускать: _bot_app is None.")
        return

    if _bot_thread is not None and _bot_thread.is_alive():
        logger.info("[BOT] Поток уже запущен, повторный запуск не нужен.")
        return

    def _runner() -> None:
        logger.info("[BOT] Polling-поток стартовал.")
        # run_polling блокирующий, но крутится в отдельном потоке
        _bot_app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            stop_signals=None,   # Управлять сигналами будет сам Render/процесс
        )
        logger.info("[BOT] Polling-поток завершился.")

    _bot_thread = threading.Thread(
        target=_runner,
        name="telegram-bot-polling",
        daemon=True,
    )
    _bot_thread.start()


def stop_bot_polling_in_thread() -> None:
    """
    На Render процесс и так будет убит, а поток — daemon.
    Делаем no-op, чтобы не городить сложную синхронизацию event loop'ов.
    """
    logger.info("[BOT] stop_bot_polling_in_thread вызван (no-op, поток завершится с процессом).")







