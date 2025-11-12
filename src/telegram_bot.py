# src/telegram_bot.py
import asyncio
import logging
import os
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    Defaults,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ----- Простейшее "хранилище" ставок в памяти (замените своей БД при желании)
_bets = []  # список строк
_PIN = os.getenv("CLEAR_PIN", "100182")  # PIN можно задать через переменную окружения


async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 Привет! Я бот.\n\n"
        "Команды:\n"
        "/addbet <текст> — добавить ставку\n"
        "/bets — показать ставки\n"
        f"/clearbets <PIN> — очистить (PIN по умолчанию {_PIN})"
    )
    await update.message.reply_text(text)


async def _cmd_addbet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("⚠️ Укажи текст ставки: /addbet <текст>")
        return
    bet_text = " ".join(context.args).strip()
    _bets.append(bet_text)
    await update.message.reply_text(f"✅ Ставка добавлена: #{len(_bets)}")


async def _cmd_bets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _bets:
        await update.message.reply_text("Пока ставок нет.")
        return
    lines = [f"#{i+1}. {b}" for i, b in enumerate(_bets)]
    await update.message.reply_text("📋 Ставки:\n" + "\n".join(lines))


async def _cmd_clearbets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Укажи PIN: /clearbets <PIN>")
        return
    pin = context.args[0]
    if pin != _PIN:
        await update.message.reply_text("❌ Неверный PIN.")
        return
    _bets.clear()
    await update.message.reply_text("🧹 Очищено.")


async def _job_refresh_line(context: ContextTypes.DEFAULT_TYPE) -> None:
    # сюда поместите парсинг линии букмекера
    logger.info("[JOB] refresh line tick")
    # пример уведомления (по желанию): если есть chat_id — отправляем
    # await context.bot.send_message(chat_id=<your_chat_id>, text="Обновил линию.")


def _make_application(token: str) -> Application:
    """
    Создаёт Application с правильной настройкой parse_mode для PTB v21.
    ВАЖНО: никаких .parse_mode(...) у билдера — только Defaults(parse_mode=...).
    """
    return (
        ApplicationBuilder()
        .token(token)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .build()
    )


async def build_bot_app() -> Optional[Application]:
    """
    Вызывается из FastAPI при старте. Возвращает Application или None,
    если токен не задан (чтобы сервис не падал).
    """
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.warning("⚠️ TELEGRAM_TOKEN не задан — бот не будет запущен.")
        return None

    app = _make_application(token)

    # Хендлеры
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("addbet", _cmd_addbet))
    app.add_handler(CommandHandler("bets", _cmd_bets))
    app.add_handler(CommandHandler("clearbets", _cmd_clearbets))

    # Периодическая задача — только если установлен extra "job-queue"
    # (иначе app.job_queue будет None)
    if getattr(app, "job_queue", None) is not None:
        # каждые 5 минут, первый запуск через 5 сек
        app.job_queue.run_repeating(_job_refresh_line, interval=300, first=5)
    else:
        logger.info("JobQueue не доступен (установите python-telegram-bot[job-queue], если нужен автопарсинг).")

    return app


async def start_polling(app: Application) -> None:
    """
    Запускает polling неблокирующе (для интеграции с FastAPI lifespan).
    """
    await app.initialize()
    await app.start()
    # start_polling внутри start() в v21 не запускается — запускаем вручную:
    await app.updater.start_polling()  # type: ignore[attr-defined]
    logger.info("[BOT] Polling запущен.")


async def stop_polling(app: Application) -> None:
    """
    Останавливает polling и бот.
    """
    try:
        await app.updater.stop()  # type: ignore[attr-defined]
    except Exception:
        pass
    await app.stop()
    await app.shutdown()
    logger.info("[BOT] Остановлен.")
# --- Совместимость со старым service.py ---
# В старом коде ожидалась функция run_polling, оставим алиас:
async def run_polling(app: Application) -> None:
    await start_polling(app)







