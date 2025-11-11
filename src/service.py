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

@app.get("/healthz")
def healthz():
    return {"ok": True, "time": datetime.utcnow().isoformat()}

class EchoReq(BaseModel):
    text: str

@app.post("/echo")
def echo(body: EchoReq):
    return {"echo": body.text}

# ---------- Telegram ----------
_TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
_bot_task: Optional[asyncio.Task] = None
_bot_app = None
_stop_event: Optional[asyncio.Event] = None

# --- handlers ---
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
    app = (
        ApplicationBuilder()
        .token(_TELEGRAM_TOKEN)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("health", _cmd_health))
    app.add_handler(CommandHandler("bets", _cmd_bets))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))
    return app

async def _run_bot_polling():
    """
    Инициализируем и запускаем PTB в уже идущем uvicorn event loop
    без run_polling(), чтобы не трогать цикл.
    """
    global _bot_app, _stop_event
    _stop_event = asyncio.Event()
    _bot_app = _build_application()

    # 1) initialize PTB (создает внутренние ресурсы)
    await _bot_app.initialize()

    # 2) start PTB (поднимает Request, JobQueue и т.п.)
    await _bot_app.start()

    # 3) запуск getUpdates-поллинга (drop старых апдейтов)
    #    ВАЖНО: здесь НЕТ собственного event loop-менеджмента
    await _bot_app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

    log.info("[BOT] Polling запущен")

    # 4) ждем сигнала остановки
    await _stop_event.wait()

    log.info("[BOT] Останавливаюсь…")

    # 5) остановка поллинга и приложения в обратном порядке
    await _bot_app.updater.stop()
    await _bot_app.stop()
    await _bot_app.shutdown()

@app.on_event("startup")
async def on_startup():
    global _bot_task
    if _bot_task is None:
        log.info("[BOT] Создаю фоновую задачу polling")
        _bot_task = asyncio.create_task(_run_bot_polling())

@app.on_event("shutdown")
async def on_shutdown():
    global _bot_task, _stop_event
    try:
        if _stop_event is not None:
            _stop_event.set()
        if _bot_task is not None and not _bot_task.done():
            # дождаться корректной остановки
            await _bot_task
    except Exception as e:
        log.warning("[BOT] Ошибка при остановке: %s", e)
    finally:
        _bot_task = None


