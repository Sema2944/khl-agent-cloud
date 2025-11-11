# src/service.py
from __future__ import annotations
import asyncio
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

# --- FastAPI приложение ---
app = FastAPI(title="KHL Agent API")

@app.get("/healthz")
def healthz():
    return {"ok": True}

# --- Telegram bot (python-telegram-bot) ---
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram import Update

API_BASE = os.getenv("API_BASE", "https://khl-agent-api.onrender.com")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Глобальная ссылка на приложение бота и задачу
_bot_app = None
_bot_task: asyncio.Task | None = None

# Команды бота (минимум)
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я KHL Agent. Команды: /bets, /refresh, /health")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Доступно: /bets — демо; /refresh — обновить; /health — пинг API")

async def bets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎯 Демо: П1 ЦСКА — Спартак, кэф 1.95, Edge 0.04, ставка 1.25")

async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Обновил (демо) ✓")

async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{API_BASE}/healthz")
            await update.message.reply_text(f"API /healthz → {r.status_code}: {r.text}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка запроса к API: {e}")

async def _run_bot_polling():
    """Фоновая задача: запустить polling и держать бота активным."""
    global _bot_app
    if not TELEGRAM_BOT_TOKEN:
        print("[BOT] TELEGRAM_BOT_TOKEN не задан — бот не будет запущен")
        return

    _bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    _bot_app.add_handler(CommandHandler("start", start_cmd))
    _bot_app.add_handler(CommandHandler("help", help_cmd))
    _bot_app.add_handler(CommandHandler("bets", bets_cmd))
    _bot_app.add_handler(CommandHandler("refresh", refresh_cmd))
    _bot_app.add_handler(CommandHandler("health", health_cmd))

    print("[BOT] Запускаю polling…")
    # run_polling — корутина; будет работать пока не отменят задачу
    await _bot_app.run_polling(allowed_updates=Update.ALL_TYPES)

# Хуки FastAPI старта/остановки
@app.on_event("startup")
async def on_startup():
    global _bot_task
    # Запускаем бота фоном, если есть токен
    if TELEGRAM_BOT_TOKEN and (_bot_task is None or _bot_task.done()):
        loop = asyncio.get_running_loop()
        _bot_task = loop.create_task(_run_bot_polling())
        print("[BOT] Фоновая задача создана")

@app.on_event("shutdown")
async def on_shutdown():
    global _bot_app, _bot_task
    # Аккуратно останавливаем бота
    try:
        if _bot_app is not None:
            await _bot_app.shutdown()
            await _bot_app.stop()
            _bot_app = None
    except Exception as e:
        print(f"[BOT] Ошибка при остановке: {e}")

    if _bot_task is not None:
        _bot_task.cancel()
        try:
            await _bot_task
        except asyncio.CancelledError:
            pass
        _bot_task = None
        print("[BOT] Фоновая задача остановлена")
