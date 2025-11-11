from __future__ import annotations
import asyncio, os
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram import Update

API_BASE = os.getenv("API_BASE", "https://khl-agent-api.onrender.com")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я KHL Agent. Команды: /bets, /refresh, /help")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Доступно: /bets — демо ставка; /refresh — обновить; /health — пинг API")

async def bets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎯 Демо: П1 ЦСКА — Спартак, кэф 1.95, Edge 0.04, ставка 1.25")

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Обновил (демо) ✓")

async def health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{API_BASE}/healthz")
            await update.message.reply_text(f"API /healthz → {r.status_code}: {r.text}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка запроса к API: {e}")

async def main():
    if not TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("bets", bets))
    app.add_handler(CommandHandler("refresh", refresh))
    app.add_handler(CommandHandler("health", health))
    await app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    asyncio.run(main())
