import asyncio
import logging
from fastapi import FastAPI
from src.telegram_bot import build_bot_app, run_polling, stop_polling

logger = logging.getLogger("svc")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="KHL Agent Cloud API")

# Держим ссылку на приложение бота
_bot_app = None
_bot_task: asyncio.Task | None = None

@app.on_event("startup")
async def on_startup():
    global _bot_app, _bot_task
    logger.info("[APP] Запуск приложения...")

    _bot_app = await build_bot_app()
    if _bot_app is None:
        logger.warning("[BOT] TELEGRAM_TOKEN отсутствует — бот не будет запущен.")
        return

    # Стартуем polling в фоне, не блокируя FastAPI
    _bot_task = asyncio.create_task(run_polling(_bot_app))
    logger.info("[BOT] Polling запущен.")

@app.on_event("shutdown")
async def on_shutdown():
    global _bot_app, _bot_task
    logger.info("[APP] Остановка приложения...")

    if _bot_app is not None:
        try:
            await stop_polling(_bot_app)
        except Exception:
            logger.exception("[BOT] Ошибка при остановке бота")

    if _bot_task and not _bot_task.done():
        _bot_task.cancel()
        try:
            await _bot_task
        except asyncio.CancelledError:
            pass

@app.get("/")
async def root():
    return {"status": "ok", "message": "KHL Agent API is running"}
