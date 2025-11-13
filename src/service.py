import asyncio
import logging
from fastapi import FastAPI
from telegram import Update

from src.telegram_bot import build_bot_app

log = logging.getLogger("svc")
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

app = FastAPI(title="KHL Agent API")

_bot_app = None
_bot_task: asyncio.Task | None = None


@app.on_event("startup")
async def on_startup():
    global _bot_app, _bot_task
    log.info("[APP] Запуск приложения...")

    # Инициализируем Telegram Application
    _bot_app = await build_bot_app()

    # Запускаем long polling в фоне
    # В PTB v21 run_polling — корутина, её можно запускать в таске.
    _bot_task = asyncio.create_task(
        _bot_app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            close_loop=False,               # не закрывать event loop FastAPI
            stop_signals=None,              # управляем остановкой сами
            drop_pending_updates=False,     # апдейты уже сброшены при delete_webhook
        )
    )
    log.info("[BOT] Long polling запущен.")


@app.on_event("shutdown")
async def on_shutdown():
    global _bot_app, _bot_task
    log.info("[APP] Остановка приложения...")

    if _bot_task:
        # Корректно остановим Application
        try:
            await _bot_app.stop()
        except Exception:
            pass
        _bot_task.cancel()
        try:
            await _bot_task
        except asyncio.CancelledError:
            pass
        _bot_task = None

    log.info("[APP] Остановка завершена.")


@app.get("/")
async def root():
    return {"status": "ok", "bot": "running" if _bot_task and not _bot_task.done() else "stopped"}

