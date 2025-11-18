# src/telegram_bot.py

import os
import logging
import asyncio
import re
from datetime import datetime

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
import httpx


API_BASE = os.getenv("API_BASE", "https://khl-agent-api.onrender.com")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# =========================================================
# Основная клавиатура
# =========================================================

def build_main_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["профиль", "мои ставки"],
        ["КХЛ сегодня", "отчёт за неделю"],
        ["разбор моих рынков", "состояние банка"],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


# =========================================================
# Клавиатура результата ставки
# =========================================================

def build_bet_result_keyboard(bet_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🟢 Выиграла", callback_data=f"BET_RES:{bet_id}:win"
                ),
                InlineKeyboardButton(
                    "🔴 Проиграла", callback_data=f"BET_RES:{bet_id}:lose"
                ),
            ],
            [
                InlineKeyboardButton(
                    "⚪ Возврат", callback_data=f"BET_RES:{bet_id}:push"
                )
            ]
        ]
    )


# =========================================================
# API calls
# =========================================================

async def call_agent(user_id: int, message: str) -> str:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{API_BASE}/agent/query",
            json={"user_id": user_id, "message": message},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("reply", "Пустой ответ от агента 😕")


async def call_last_bets(user_id: int, limit: int = 5) -> list[dict]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{API_BASE}/agent/last-bets",
            params={"user_id": user_id, "limit": limit},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("bets", []) or []


# =========================================================
# /start
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    text = (
        "Привет! Я AI-помощник для ставок на хоккей 🏒\n\n"
        "Я умею:\n"
        "• Вести историю ставок и статистику\n"
        "• Обновлять банк и считать ROI\n"
        "• Разбирать матчи КХЛ\n"
        "• Делать отчёты и анализ твоих рынков\n\n"
        "Пиши мне сообщения или используй кнопки ниже 👇"
    )

    await update.message.reply_text(
        text,
        reply_markup=build_main_keyboard(),
    )


# =========================================================
# Форматирование данных ставки
# =========================================================

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
            dt_str = datetime.fromisoformat(created_raw).strftime("%d.%m %H:%M")
        except Exception:
            dt_str = created_raw

    lines = []
    header = f"Ставка #{bet_id}"
    if dt_str:
        header += f" от {dt_str}"
    lines.append(header)

    if event:
        lines.append(f"Событие: {event}")
    if outcome:
        lines.append(f"Исход: {outcome}")
    if stake is not None:
        lines.append(f"Сумма: {stake:.0f}")
    if odds is not None:
        lines.append(f"Коэффициент: {odds:.2f}")

    if result:
        if result == "win":
            human = "выигрыш"
        elif result == "lose":
            human = "проигрыш"
        elif result == "push":
            human = "возврат"
        else:
            human = result

        res_line = f"Результат: {human}"
        if profit is not None:
            sign = "+" if profit >= 0 else ""
            res_line += f", PnL: {sign}{profit:.0f}"
        lines.append(res_line)

    return "\n".join(lines)


# =========================================================
# Handle text messages
# =========================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    norm = text.lower()

    # -----------------------------------------
    # "мои ставки" → структурированный вывод
    # -----------------------------------------
    if "мои ставки" in norm:
        try:
            bets = await call_last_bets(user_id, limit=5)
        except Exception as e:
            logger.exception("Ошибка /agent/last-bets: %s", e)
            await update.message.reply_text(
                "Не удалось получить последние ставки 😔",
                reply_markup=build_main_keyboard(),
            )
            return

        if not bets:
            await update.message.reply_text(
                "У тебя пока нет сохранённых ставок.",
                reply_markup=build_main_keyboard(),
            )
            return

        await update.message.reply_text(
            "Твои последние ставки:",
            reply_markup=build_main_keyboard(),
        )

        for b in bets:
            txt = _format_bet_for_user(b)
            bet_id = b.get("id")
            result = b.get("result")

            if result is None:
                await update.message.reply_text(
                    txt,
                    reply_markup=build_bet_result_keyboard(bet_id),
                )
            else:
                await update.message.reply_text(txt)

        return

    # -----------------------------------------
    # Отправка текста в /agent/query
    # -----------------------------------------
    try:
        reply = await call_agent(user_id, text)
    except Exception as e:
        logger.exception("Ошибка call_agent: %s", e)
        await update.message.reply_text(
            "Не удалось связаться с сервером 😔",
            reply_markup=build_main_keyboard(),
        )
        return

    # Если в ответе есть id ставки — прикрепляем кнопки
    m = re.search(r"Ставка сохранена \(id:\s*(\d+)\)", reply)
    if m:
        bet_id = int(m.group(1))
        await update.message.reply_text(
            reply,
            reply_markup=build_bet_result_keyboard(bet_id),
        )
        return

    # Обычный ответ
    await update.message.reply_text(
        reply,
        reply_markup=(
            build_main_keyboard() if norm in {"/start", "start", "help", "/help", "меню"} else None
        ),
    )


# =========================================================
# Callback handler (кнопки WIN / LOSE / PUSH)
# =========================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    raw = query.data or ""

    if not raw.startswith("BET_RES:"):
        return

    _, bet_id_str, status = raw.split(":")
    bet_id = int(bet_id_str)
    user_id = query.from_user.id

    # Составляем текст, как будто пользователь ввёл его вручную
    if status == "win":
        cmd = f"ставка {bet_id} выиграла"
        label = "выигрыш"
    elif status == "lose":
        cmd = f"ставка {bet_id} проиграла"
        label = "проигрыш"
    else:
        cmd = f"ставка {bet_id} возврат"
        label = "возврат"

    try:
        agent_reply = await call_agent(user_id, cmd)
    except Exception:
        agent_reply = "Ошибка при обновлении результата 😔"

    # Обновляем сообщение (убираем кнопки)
    try:
        msg = query.message.text or ""
        await query.edit_message_text(msg + f"\n\n✓ Результат: {label}")
    except Exception:
        pass

    # Отправляем новое сообщение с подробностями
    await query.message.reply_text(agent_reply)


# =========================================================
# MAIN
# =========================================================

def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")

    logger.info("Запускаю Telegram-бота...")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(BOT_TOKEN).build()

    # handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling(stop_signals=None)
