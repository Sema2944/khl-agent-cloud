# src/telegram_bot.py
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
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logger = logging.getLogger(__name__)

# -------------------- ENV --------------------

BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
API_BASE = (os.getenv("API_BASE") or "").strip()

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
if not API_BASE:
    raise RuntimeError("API_BASE is not set")

logger.info("Using backend API_BASE=%r", API_BASE)


# -------------------- Keyboards --------------------

def build_main_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["профиль", "мои ставки"],
        ["КХЛ сегодня", "отчёт за неделю"],
        ["разбор моих рынков", "состояние банка"],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


def build_bet_result_keyboard(bet_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🟢 Выиграла", callback_data=f"BET_RES:{bet_id}:win"),
                InlineKeyboardButton("🔴 Проиграла", callback_data=f"BET_RES:{bet_id}:lose"),
            ],
            [
                InlineKeyboardButton("⚪️ Возврат", callback_data=f"BET_RES:{bet_id}:push"),
            ],
        ]
    )


# -------------------- Backend API --------------------

async def call_agent(user_id: int, message: str) -> str:
    """
    POST {API_BASE}/agent/query
    Body: {"user_id": ..., "message": "..."}  (service.py поддерживает message/query)
    """
    payload = {"user_id": int(user_id), "message": (message or "").strip()}
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(f"{API_BASE}/agent/query", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("reply") or "Пустой ответ от сервера 😕"


async def call_last_bets(user_id: int, limit: int = 5) -> list[dict]:
    """
    GET {API_BASE}/agent/last-bets?user_id=...&limit=...
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            f"{API_BASE}/agent/last-bets",
            params={"user_id": int(user_id), "limit": int(limit)},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("bets", []) or []


# -------------------- Helpers --------------------

def _format_bet_for_user(b: dict) -> str:
    bet_id = b.get("id")
    created_raw = b.get("created_at")
    event = b.get("event")
    outcome = b.get("outcome")
    stake = b.get("stake")
    odds = b.get("odds")
    result = b.get("result")
    profit = b.get("profit")

    dt_str = ""
    if created_raw:
        try:
            dt = datetime.fromisoformat(created_raw)
            dt_str = dt.strftime("%d.%m %H:%M")
        except Exception:
            dt_str = str(created_raw)

    lines: list[str] = []
    header = f"Ставка #{bet_id}" if bet_id is not None else "Ставка"
    if dt_str:
        header += f" от {dt_str}"
    lines.append(header)

    if event:
        lines.append(f"Событие: {event}")
    if outcome:
        lines.append(f"Исход: {outcome}")

    if stake is not None:
        try:
            lines.append(f"Сумма: {float(stake):.0f}")
        except Exception:
            lines.append(f"Сумма: {stake}")

    if odds is not None:
        try:
            lines.append(f"Коэффициент: {float(odds):.2f}")
        except Exception:
            lines.append(f"Коэффициент: {odds}")

    if result:
        mapping = {"win": "выигрыш", "lose": "проигрыш", "push": "возврат"}
        human = mapping.get(result, result)
        line = f"Результат: {human}"
        if profit is not None:
            try:
                p = float(profit)
                sign = "+" if p >= 0 else ""
                line += f", PnL: {sign}{p:.0f}"
            except Exception:
                line += f", PnL: {profit}"
        lines.append(line)

    return "\n".join(lines)


# -------------------- Commands --------------------

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text("✅ Бот работает.", reply_markup=build_main_keyboard())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    text = (
        "Привет! Я AI-агент по хоккею 🏒\n\n"
        "Доступные команды (MVP):\n"
        "• профиль\n"
        "• мои ставки\n"
        "• КХЛ сегодня\n"
        "• отчёт за неделю\n"
        "• разбор моих рынков\n"
        "• состояние банка\n\n"
        "Также можешь писать:\n"
        "• `мой банк 100000`\n"
        "• `ставка: <событие>; исход=...; сумма=...; кэф=...`\n"
    )
    await update.message.reply_text(text, reply_markup=build_main_keyboard())


# -------------------- Message handler --------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    norm = text.lower().strip()

    logger.info("handle_message: user_id=%s, text=%r", user_id, text)

    # 1) Мои ставки — берём отдельным эндпоинтом (быстро)
    if "мои ставки" in norm:
        try:
            bets = await call_last_bets(user_id, 5)
        except Exception:
            logger.exception("call_last_bets failed")
            await update.message.reply_text("Не удалось получить ставки 😔", reply_markup=build_main_keyboard())
            return

        if not bets:
            await update.message.reply_text("У тебя нет сохранённых ставок.", reply_markup=build_main_keyboard())
            return

        await update.message.reply_text("Твои последние ставки:", reply_markup=build_main_keyboard())
        for b in bets:
            msg = _format_bet_for_user(b)
            if b.get("result") is None and b.get("id") is not None:
                await update.message.reply_text(msg, reply_markup=build_bet_result_keyboard(int(b["id"])))
            else:
                await update.message.reply_text(msg)
        return

    # 2) Всё остальное — через /agent/query (включая профиль, банк, отчёты, КХЛ сегодня)
    try:
        reply = await call_agent(user_id, text)
    except Exception:
        logger.exception("call_agent failed")
        await update.message.reply_text("Не удалось связаться с backend 😔", reply_markup=build_main_keyboard())
        return

    # 3) Если агент сохранил ставку — показываем кнопки результата
    m = re.search(r"Ставка сохранена \(id:\s*(\d+)\)", reply)
    if m:
        bet_id = int(m.group(1))
        await update.message.reply_text(reply, reply_markup=build_bet_result_keyboard(bet_id))
        return

    await update.message.reply_text(reply, reply_markup=build_main_keyboard())


# -------------------- Callback buttons --------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    await query.answer()

    if not data.startswith("BET_RES:"):
        return

    _, bet_id_str, res = data.split(":")
    bet_id = int(bet_id_str)
    user_id = query.from_user.id

    mapping = {"win": "выигрыш", "lose": "проигрыш", "push": "возврат"}
    cmd = {
        "win": f"ставка {bet_id} выиграла",
        "lose": f"ставка {bet_id} проиграла",
        "push": f"ставка {bet_id} возврат",
    }[res]

    try:
        agent_reply = await call_agent(user_id, cmd)
    except Exception:
        logger.exception("call_agent failed on settle")
        agent_reply = "Ошибка связи с сервером 😔"

    # убираем кнопки
    original = query.message.text or ""
    new_text = original + f"\n\n✅ Результат отмечен: {mapping[res]}."
    await query.edit_message_text(new_text)

    await query.message.reply_text(agent_reply, reply_markup=build_main_keyboard())


# -------------------- Error handler --------------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error: %s", context.error)


# -------------------- MAIN --------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_error_handler(on_error)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # ВАЖНО: polling должен быть только в одном месте (или локально, или Render)
    app.run_polling(stop_signals=None)


if __name__ == "__main__":
    main()
