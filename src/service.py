import asyncio
import logging

from fastapi import FastAPI

from src.telegram_bot import build_bot_app, start_bot_polling, stop_bot_polling

# Если у тебя есть init_db в src/db.py — можно раскомментировать:
# from src.db import init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("svc")

app = FastAPI(title="khl-agent-api")

# Глобальные ссылки на приложение бота и таску polling
_bot_app = None
_bot_task: asyncio.Task | None = None


@app.on_event("startup")
async def on_startup() -> None:
    global _bot_app, _bot_task

    logger.info("[APP] Запуск приложения...")

    # Если используется БД, можешь включить:
    # try:
    #     await init_db()
    #     logger.info("[DB] Инициализирована.")
    # except Exception:
    #     logger.exception("[DB] Ошибка инициализации.")

    # Создаём Telegram Application
    _bot_app = build_bot_app()
    if _bot_app is None:
        logger.warning("[BOT] TELEGRAM_TOKEN отсутствует — бот не будет запущен.")
        return

    # Запускаем polling в фоне, чтобы не блокировать event loop FastAPI
    loop = asyncio.get_event_loop()
    _bot_task = loop.create_task(start_bot_polling(_bot_app))
    logger.info("[BOT] Задача polling запущена.")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    global _bot_app, _bot_task

    logger.info("[APP] Остановка приложения...")

    if _bot_app is not None:
        try:
            await stop_bot_polling(_bot_app)
        except Exception:
            logger.exception("[BOT] Ошибка при остановке polling.")

    if _bot_task is not None:
        _bot_task.cancel()
        _bot_task = None

    logger.info("[APP] Остановка завершена.")


@app.get("/")
async def root():
    """Простой health-check, чтобы Render видел, что сервис живой."""
    return {"status": "ok", "service": "khl-agent-api", "bot_running": _bot_app is not None}




