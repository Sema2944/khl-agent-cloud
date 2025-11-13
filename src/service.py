import logging

from fastapi import FastAPI

from src.db import init_db
from src.telegram_bot import build_bot_app, start_bot_polling_in_thread, stop_bot_polling_in_thread

logger = logging.getLogger("svc")

app = FastAPI(title="KHL Agent API")


@app.on_event("startup")
async def on_startup() -> None:
    logger.info("[APP] Запуск приложения...")

    # Инициализация БД (как у тебя было)
    try:
        await init_db()
        logger.info("[DB] init_db выполнен.")
    except Exception as e:
        logger.exception(f"[DB] Ошибка init_db: {e}")

    # Инициализация бота
    bot_app = await build_bot_app()
    if bot_app is None:
        logger.warning("[BOT] TELEGRAM_TOKEN отсутствует — бот не будет запущен.")
    else:
        logger.info("[BOT] Application создан, запускаем polling в отдельном потоке...")
        start_bot_polling_in_thread()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    logger.info("[APP] Остановка приложения...")

    # Сейчас stop_bot_polling_in_thread — no-op, просто логирует
    try:
        stop_bot_polling_in_thread()
    except Exception as e:
        logger.exception(f"[BOT] Ошибка при остановке бота: {e}")

    logger.info("[APP] Остановка завершена.")


@app.get("/")
async def root():
    return {"status": "ok", "message": "KHL Agent API is running"}





