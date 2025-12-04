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

# Логгер
logger = logging.getLogger(__name__)

# Токен бота
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Берём API_BASE только из переменной окружения
API_BASE = os.getenv("API_BASE")
if not API_BASE:
    # Если переменная не задана — останавливаем воркер
    raise RuntimeError("API_BASE environment variable is not set!")

logger.info("Using backend API_BASE=%s", API_BASE)

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


# ----------------------- API -----------------------

async def call_agent(user_id: int, message: str) -> str:
    """
    ВАЖНО: backend ждёт поле 'query', а не 'message'
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{API_BASE}/agent/query",
            json={"user_id": user_id, "query": message},
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


# ----------------------- Команды -----------------------

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "✅ Бот работает. Если что-то не отвечает — проблема в backend."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    text = (
        "Привет! Я AI-агент для ставок на хоккей 🏒\n\n"
        "Я умею:\n"
        "• Вести историю ставок и статистику (winrate, ROI, PnL)\n"
        "• Работать с банк-менеджментом\n"
        "• Делать отчёты за неделю и разбор твоих рынков\n"
        "• Показывать матчи КХЛ на сегодня и разбирать матч\n\n"
        "Нажимай кнопки внизу или напиши мне что-нибудь 😉"
    )

    await update.message.reply_text(text, reply_markup=build_main_keyboard())


# ----------------------- Форматирование ставки -----------------------

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
        except:
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
        try:
            lines.append(f"Сумма: {float(stake):.0f}")
        except:
            lines.append(f"Сумма: {stake}")

    if odds is not None:
        try:
            lines.append(f"Коэффициент: {float(odds):.2f}")
        except:
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
            except:
                line += f", PnL: {profit}"
        lines.append(line)

    return "\n".join(lines)


# ----------------------- Обработка текстов -----------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    user_id = update.effective_user.id
    text = update.message.text
    norm = text.lower().strip()

    logger.info("handle_message: user_id=%s, text=%r", user_id, text)

    # ---- Мои ставки ----
    if "мои ставки" in norm:
        try:
            bets = await call_last_bets(user_id, 5)
        except Exception:
            await update.message.reply_text(
                "Не удалось получить ставки 😔", reply_markup=build_main_keyboard()
            )
            return

        if not bets:
            await update.message.reply_text(
                "У тебя нет сохранённых ставок.", reply_markup=build_main_keyboard()
            )
            return

        await update.message.reply_text("Твои последние ставки:", reply_markup=build_main_keyboard())

        for b in bets:
            text_m = _format_bet_for_user(b)
            if b.get("result") is None:
                await update.message.reply_text(text_m, reply_markup=build_bet_result_keyboard(b["id"]))
            else:
                await update.message.reply_text(text_m)
        return

    # ---- обычный запрос ----
    try:
        reply = await call_agent(user_id, text)
    except Exception:
        await update.message.reply_text(
            "Не удалось связаться с backend 😔", reply_markup=build_main_keyboard()
        )
        return

    # Если агент вернул "Ставка сохранена (id: X)"
    m = re.search(r"Ставка сохранена \(id:\s*(\d+)\)", reply)
    if m:
        bet_id = int(m.group(1))
        await update.message.reply_text(reply, reply_markup=build_bet_result_keyboard(bet_id))
        return

    # Ответ с клавиатурой, если это меню
    if norm in {"/start", "start", "меню", "help", "/help"}:
        await update.message.reply_text(reply, reply_markup=build_main_keyboard())
    else:
        await update.message.reply_text(reply)


# ----------------------- Callback-кнопки -----------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    data = query.data
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
        agent_reply = "Ошибка связи с сервером 😔"

    # убираем кнопки
    original = query.message.text or ""
    new_text = original + f"\n\n✅ Результат отмечен: {mapping[res]}."
    await query.edit_message_text(new_text)

    await query.message.reply_text(agent_reply)


# ----------------------- MAIN -----------------------

def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")

    logger.info("Запускаю Telegram-бота...")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling(stop_signals=None)


if __name__ == "__main__":
    main()
