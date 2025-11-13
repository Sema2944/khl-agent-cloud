import logging

from fastapi import FastAPI

from src.db import init_db
from src.telegram_bot import build_bot_app, start_bot_polling, stop_bot_polling

logger = logging.getLogger("svc")

app = FastAPI(title="KHL Agent API")


@app.on_event("startup")
async def on_startup() -> None:
    logger.info("[APP] Запуск приложения...")

    # Инициализируем БД
    try:
        await init_db()
        logger.info("[DB] init_db выполнен.")
    except Exception as e:
        logger.exception("[DB] Ошибка init_db: %s", e)

    # Инициализируем и запускаем бота
    bot_app = await build_bot_app()
    if bot_app is None:
        logger.warning("[BOT] TELEGRAM_TOKEN отсутствует — бот не будет запущен.")
    else:
        logger.info("[BOT] Запускаем polling...")
        await start_bot_polling()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    logger.info("[APP] Остановка приложения...")

    try:
        await stop_bot_polling()
    except Exception as e:
        logger.exception("[BOT] Ошибка при остановке бота: %s", e)

    logger.info("[APP] Остановка завершена.")


@app.get("/")
async def root():
    return {"status": "ok", "message": "KHL Agent API is running"}






