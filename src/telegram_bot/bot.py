from __future__ import annotations

import os
import logging
import asyncio
import re
from datetime import datetime

import httpx
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
API_BASE = (os.getenv("API_BASE") or "").strip().rstrip("/")
BACKEND_TIMEOUT = float((os.getenv("BACKEND_TIMEOUT") or "8").strip())

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

if not API_BASE:
    logger.warning("API_BASE is not set. Bot will work in 'no-backend' mode.")

# =========================
# ГЛАВНОЕ МЕНЮ (ОДНО!)
# =========================
MAIN_KB = ReplyKeyboardMarkup(
    [
        ["🧠 AI Аналитика", "👤 Стратегия эксперта"],
        ["🏟 Матчи сегодня", "📊 Профиль"],
        ["📒 Мои ставки", "📆 Отчёт за неделю"],
        ["📉 Разбор моих рынков", "🏦 Состояние банка"],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)

# =========================
# INLINE под матчем
# =========================
def build_match_actions_keyboard(match_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📈 Линия", callback_data=f"MATCH_LINE:{match_id}"),
                InlineKeyboardButton("🧠 AI разбор", callback_data=f"MATCH_AI:{match_id}"),
            ],
            [
                InlineKeyboardButton(
                    "👤 Мнение эксперта", callback_data=f"MATCH_EXPERT:{match_id}"
                )
            ],
        ]
    )


def build_bet_result_keyboard(bet_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🟢 Выиграла", callback_data=f"BET_RES:{bet_id}:win"),
                InlineKeyboardButton("🔴 Проиграла", callback_data=f"BET_RES:{bet_id}:lose"),
            ],
            [InlineKeyboardButton("⚪️ Возврат", callback_data=f"BET_RES:{bet_id}:push")],
        ]
    )


# =========================
# BACKEND
# =========================
async def _safe_request(method: str, url: str, **kwargs) -> dict:
    timeout = kwargs.pop("timeout", BACKEND_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.request(method, url, **kwargs)
        r.raise_for_status()
        return r.json()


async def call_agent(user_id: int, message: str) -> str:
    if not API_BASE:
        return "Backend не настроен."
    payload = {"user_id": user_id, "message": message}
    data = await _safe_request("POST", f"{API_BASE}/agent/query", json=payload)
    return data.get("reply", "Пустой ответ 😕")


async def call_last_bets(user_id: int, limit: int = 5) -> list[dict]:
    if not API_BASE:
        return []
    data = await _safe_request(
        "GET",
        f"{API_BASE}/agent/last-bets",
        params={"user_id": user_id, "limit": limit},
    )
    return data.get("bets", []) or []


# =========================
# UTILS
# =========================
def _normalize(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"[^\w\sа-яё]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _format_bet(b: dict) -> str:
    lines = [f"Ставка #{b.get('id')}"]
    if b.get("event"):
        lines.append(f"Событие: {b['event']}")
    if b.get("outcome"):
        lines.append(f"Исход: {b['outcome']}")
    if b.get("stake") is not None:
        lines.append(f"Сумма: {b['stake']}")
    if b.get("odds") is not None:
        lines.append(f"Коэф: {b['odds']}")
    if b.get("result"):
        lines.append(f"Результат: {b['result']}")
    return "\n".join(lines)


# =========================
# HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Бот запущен.\nВыбирай действие кнопками ниже.",
        reply_markup=MAIN_KB,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""
    norm = _normalize(text)

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    # --- Главное меню ---
    if norm in {"стратегия эксперта", "стратегия"}:
        reply = await call_agent(user_id, "стратегия")
        await update.message.reply_text(reply, reply_markup=MAIN_KB)
        return

    if norm in {"ai аналитика", "аналитика"}:
        await update.message.reply_text(
            "Напиши:\n`аналитика <id матча>` или `аналитика <вопрос>`",
            reply_markup=MAIN_KB,
        )
        return

    if norm in {"матчи сегодня", "матчи"}:
        reply = await call_agent(user_id, "матчи сегодня")
        m = re.search(r"id:\s*([a-zA-Z0-9_\-:.]+)", reply)
        if m:
            await update.message.reply_text(reply, reply_markup=build_match_actions_keyboard(m.group(1)))
        else:
            await update.message.reply_text(reply)
        await update.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    if norm == "мои ставки":
        bets = await call_last_bets(user_id)
        if not bets:
            await update.message.reply_text("Ставок нет.", reply_markup=MAIN_KB)
            return
        for b in bets:
            await update.message.reply_text(_format_bet(b))
        await update.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    mapping = {
        "профиль": "профиль",
        "отчёт за неделю": "отчёт за неделю",
        "разбор моих рынков": "разбор моих рынков",
        "состояние банка": "состояние банка",
    }
    if norm in mapping:
        reply = await call_agent(user_id, mapping[norm])
        await update.message.reply_text(reply, reply_markup=MAIN_KB)
        return

    # --- fallback ---
    reply = await call_agent(user_id, text)
    await update.message.reply_text(reply, reply_markup=MAIN_KB)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data.startswith("MATCH_LINE:"):
        match_id = data.split(":", 1)[1]
        text = await call_agent(q.from_user.id, f"линия {match_id}")
        await q.message.reply_text(text, reply_markup=MAIN_KB)
        return

    if data.startswith("MATCH_AI:"):
        match_id = data.split(":", 1)[1]
        text = await call_agent(q.from_user.id, f"аналитика {match_id}")
        await q.message.reply_text(text, reply_markup=MAIN_KB)
        return

    if data.startswith("MATCH_EXPERT:"):
        text = await call_agent(q.from_user.id, "стратегия")
        await q.message.reply_text(text, reply_markup=MAIN_KB)
        return


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(stop_signals=None)


if __name__ == "__main__":
    main()
