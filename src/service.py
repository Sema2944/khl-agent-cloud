# src/service.py
import os
import asyncio
import logging
from datetime import datetime
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("svc")

app = FastAPI(title="KHL Agent API")

# ---------- Простые ручки ----------
@app.get("/healthz")
def healthz():
    return {"ok": True, "time": datetime.utcnow().isoformat()}

class EchoReq(BaseModel):
    text: str

@app.post("/echo")
def echo(body: EchoReq):
    return {"echo": body.text}

# ---------- Telegram Bot ----------
_TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
_bot_task: Optional[asyncio.Task] = None
_bot_app = None  # PTB Application

async def _cmd_start(update: Update, context):
    await update.message.reply_text(
        "Привет! 🤖 Бот запущен и работает.\n\n"
        "Команды:\n"
        "/health — проверка\n"
        "/bets — показать активные ставки (пока заглушка)\n"
        "/addbet <текст> — добавить ставку (заглушка)\n"
        "/clearbets <PIN> — очистить (заглушка)"
    )

async def _cmd_health(update: Update, context):
    await update.message.reply_text("✅ Всё работает нормально!")

async def _cmd_bets(update: Update, context):
    await update.message.reply_text("📊 Пока нет активных ставок.")

async def _on_text(update: Update, context):
    if update.message and update.message.text:
        await update.message.reply_text(f"Ты написал: {update.message.text}")

def _build_application():
    if not _TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в переменных окружения")
    return (
        ApplicationBuilder()
        .token(_TELEGRAM_TOKEN)
        .concurrent_updates(True)  # немного повышает устойчивость
        .build()
    )

async def _run_bot_polling():
    """
    Фоновая корутина polling’а. Делает мягкие ретраи при Conflict/сетевых сбоях.
    Не закрывает event loop uvicorn (close_loop=False).
    """
    global _bot_app
    _bot_app = _build_application()

    # хэндлеры
    _bot_app.add_handler(CommandHandler("start", _cmd_start))
    _bot_app.add_handler(CommandHandler("health", _cmd_health))
    _bot_app.add_handler(CommandHandler("bets", _cmd_bets))
    _bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))

    backoff = 1
    max_backoff = 30

    while True:
        try:
            log.info("[BOT] Запускаю polling…")
            # ВАЖНО: drop_pending_updates=True — чтобы обрубить незавершённые старые getUpdates
            await _bot_app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                stop_signals=None,     # управляем остановкой сами
                close_loop=False,      # НЕ закрывать uvicorn loop
                drop_pending_updates=True,
            )
            log.info("[BOT] Polling завершён штатно")
            return
        except Exception as e:
            msg = str(e)
            # Наиболее частая — Conflict: другой getUpdates
            if "Conflict" in msg or "terminated by other getUpdates request" in msg:
                log.warning("[BOT] Conflict: бот уже запущен где-то ещё. Проверяю через %ss…", backoff)
            else:
                log.exception("[BOT] Ошибка в run_polling: %s", msg)

            await asyncio.sleep(backoff)
            backoff = min(max_backoff, backoff * 2)

@app.on_event("startup")
async def on_startup():
    global _bot_task
    if _bot_task is None:
        log.info("[BOT] Фоновая задача создана")
        _bot_task = asyncio.create_task(_run_bot_polling())

@app.on_event("shutdown")
async def on_shutdown():
    """
    Корректно останавливаем polling, не трогая event loop.
    """
    global _bot_task, _bot_app
    try:
        if _bot_app is not None:
            # мягкая остановка
            await _bot_app.stop()
            await _bot_app.shutdown()
            log.info("[BOT] Application остановлен")
    except Exception as e:
        log.warning("[BOT] Ошибка при остановке: %s", e)

    if _bot_task is not None:
        # отменим задачу, если она ещё активна
        if not _bot_task.done():
            _bot_task.cancel()
            try:
                await _bot_task
            except asyncio.CancelledError:
                pass
        _bot_task = None

