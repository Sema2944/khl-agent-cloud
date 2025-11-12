import asyncio
import logging
from fastapi import FastAPI
from src.telegram_bot import build_bot_app

logger = logging.getLogger("svc")
app = FastAPI(title="KHL Agent Cloud API")

@app.on_event("startup")
async def on_startup():
    logger.info("[APP] Запуск приложения...")
    try:
        _bot_app = await build_bot_app()
        if _bot_app:
            logger.info("[BOT] Telegram бот найден, инициализация...")
            asyncio.create_task(_bot_app.initialize())
            asyncio.create_task(_bot_app.start())
        else:
            logger.warning("[BOT] TELEGRAM_TOKEN не найден — бот не будет запущен.")
    except Exception as e:
        logger.exception(f"[BOT] Ошибка запуска Telegram бота: {e}")

@app.get("/")
async def root():
    return {"status": "ok", "message": "KHL Agent API is running"}


