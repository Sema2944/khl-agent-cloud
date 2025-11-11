from __future__ import annotations

import os
import asyncio
import logging

from fastapi import FastAPI
from .db import init_db
from .telegram_bot import build_bot_app

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("svc")

app = FastAPI(title="KHL Agent API")

_bot_app = None
_bot_task: asyncio.Task | None = None

@app.on_event("startup")
async def on_startup():
    global _bot_app, _bot_task
    log.info("[APP] Запуск приложения...")
    await init_db()
    _bot_app = await build_bot_app()

    # запускаем polling в фоне на том же loop
    async def _run():
        try:
            await _bot_app.initialize()
            await _bot_app.start()
            await _bot_app.updater.start_polling(allowed_updates=None, drop_pending_updates=True)
            await asyncio.Event().wait()  # вечное ожидание
        except Exception as e:
            log.exception("Polling failed: %s", e)

    _bot_task = asyncio.create_task(_run())

@app.on_event("shutdown")
async def on_shutdown():
    global _bot_app, _bot_task
    log.info("[APP] Остановка приложения...")
    if _bot_task:
        _bot_task.cancel()
        with contextlib.suppress(Exception):
            await _bot_task
    if _bot_app:
        with contextlib.suppress(Exception):
            await _bot_app.updater.stop()
        with contextlib.suppress(Exception):
            await _bot_app.stop()
        with contextlib.suppress(Exception):
            await _bot_app.shutdown()

@app.get("/health")
async def health():
    return {"ok": True}


