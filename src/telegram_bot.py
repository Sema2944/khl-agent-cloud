from __future__ import annotations
import os, asyncio, pandas as pd, requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

API_BASE = os.getenv("API_BASE", "https://khl-agent-api.onrender.com")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я KHL Agent. Команды: /bets, /refresh, /help")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Напиши /bets — пришлю демо-подборку. /refresh — переобновлю данные (демо).")

def _demo_rows():
    # мини-демо строка для /bets без внешних ключей
    return [{
        "game_id": "demo1","date":"2025-11-10","team_id":"ЦСКА","opp_id":"Спартак","is_home":1,
        "odds_1":1.95,"odds_x":3.60,"odds_2":3.50,"market":"1X2_60","selection":"best"
    }]

def _call_bets(rows, edge_min=0.02, kelly_k=0.25, max_picks=5):
    url = f"{API_BASE}/khl/bets_1x2"
    payload = {"rows": rows, "edge_min": edge_min, "kelly_k": kelly_k, "max_picks": max_picks}
    r = requests.post(url, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()

def _fmt(p: dict) -> str:
    m = {"1":"П1 (осн.)","X":"Ничья (осн.)","2":"П2 (осн.)"}
    return (f"Матч: {p['home']} — {p['away']}\n"
            f"Исход: {m.get(p['selection'], p['selection'])}\n"
            f"Коэф.: {p['odds']:.2f}\n"
            f"Модель p: {p['p_model']:.3f}\n"
            f"Edge: {p['edge']:.3f}\n"
            f"Ставка: {p['stake']:.2f}")

async def bets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Считаю (демо)…")
    try:
        rows = _demo_rows()  # заменим на реальные снапшоты, когда подключим ключи
        resp = _call_bets(rows)
        picks = resp.get("picks", [])
        if not picks:
            await update.message.reply_text("Пока без value. Попробуй позже.")
            return
        text = "🎯 Рекомендации:\n\n" + "\n\n".join(_fmt(p) for p in picks[:5])
        await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Обновляю (демо)… Готово ✓")

async def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("bets", bets))
    app.add_handler(CommandHandler("refresh", refresh))
    await app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    asyncio.run(main())
