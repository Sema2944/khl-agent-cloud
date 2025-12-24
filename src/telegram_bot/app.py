# src/telegram_bot/app.py
from __future__ import annotations

import logging
import os
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

logger = logging.getLogger(__name__)

BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
API_BASE = (os.getenv("API_BASE") or "").strip().rstrip("/")
BACKEND_TIMEOUT = float((os.getenv("BACKEND_TIMEOUT") or "8").strip())

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")


# --- Главное меню (одна клавиатура) ---
MAIN_KB = ReplyKeyboardMarkup(
    [
        ["🏟 Матчи сегодня", "🧠 AI Аналитика"],
        ["👤 Стратегия эксперта", "📊 Профиль"],
        ["📒 Мои ставки", "📆 Отчёт за неделю"],
        ["📉 Разбор моих рынков", "🏦 Состояние банка"],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)


def build_match_actions_keyboard(match_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📈 Линия", callback_data=f"MATCH_LINE:{match_id}"),
                InlineKeyboardButton("🧠 AI разбор", callback_data=f"MATCH_AI:{match_id}"),
            ],
            [
                InlineKeyboardButton(
                    "👤 Мнение эксперта (если есть)", callback_data=f"MATCH_EXPERT:{match_id}"
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


async def _safe_request(method: str, url: str, **kwargs) -> dict:
    timeout = kwargs.pop("timeout", BACKEND_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.request(method, url, **kwargs)
        r.raise_for_status()
        return r.json()


async def call_agent(user_id: int, message: str) -> str:
    if not API_BASE:
        return "Backend не настроен (API_BASE пуст). Проверь переменные окружения на Render."
    payload = {"user_id": user_id, "message": message}
    data = await _safe_request("POST", f"{API_BASE}/agent/query", json=payload, timeout=BACKEND_TIMEOUT)
    return data.get("reply", "Пустой ответ от сервера 😕")


async def call_last_bets(user_id: int, limit: int = 5) -> list[dict]:
    if not API_BASE:
        return []
    data = await _safe_request(
        "GET",
        f"{API_BASE}/agent/last-bets",
        params={"user_id": user_id, "limit": limit},
        timeout=BACKEND_TIMEOUT,
    )
    return data.get("bets", []) or []


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


def _normalize_menu_text(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[^\w\sа-яё-]", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "✅ Я на связи.\n\nВыбирай действие кнопками ниже.",
            reply_markup=MAIN_KB,
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    user_id = update.effective_user.id
    text = update.message.text or ""
    norm = _normalize_menu_text(text)

    logger.info("handle_message user_id=%s text=%r", user_id, text)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # --- меню ---
    if norm in {"стратегия эксперта", "стратегия", "эксперт", "эксперт сегодня"}:
        reply = await call_agent(user_id, "стратегия")
        await update.message.reply_text(reply, reply_markup=MAIN_KB)
        return

    if norm in {"ai аналитика", "аналитика", "ии аналитика"}:
        await update.message.reply_text(
            "Напиши:\n`аналитика <match_id> <market_key>`\nили сначала: `матчи сегодня hockey` → матч → рынок.",
            reply_markup=MAIN_KB,
        )
        return

    if norm in {"матчи сегодня"}:
        # по умолчанию показываем хоккей, дальше расширишь UI
        reply = await call_agent(user_id, "матчи сегодня hockey")
        # вытаскиваем первый match_id, чтобы под ним дать inline
        m = re.search(r"id:\s*`?([a-zA-Z0-9_\-:.]{4,80})`?", reply)
        if m:
            match_id = m.group(1)
            await update.message.reply_text(reply, reply_markup=build_match_actions_keyboard(match_id))
            await update.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
            return
        await update.message.reply_text(reply, reply_markup=MAIN_KB)
        return

    if norm in {"мои ставки"}:
        bets = await call_last_bets(user_id, 5)
        if not bets:
            await update.message.reply_text("Ставок нет.", reply_markup=MAIN_KB)
            return
        await update.message.reply_text("Твои последние ставки:", reply_markup=MAIN_KB)
        for b in bets:
            msg = _format_bet_for_user(b)
            if b.get("result") is None and b.get("id") is not None:
                await update.message.reply_text(msg, reply_markup=build_bet_result_keyboard(int(b["id"])))
            else:
                await update.message.reply_text(msg, reply_markup=MAIN_KB)
        return

    mapping = {
        "профиль": "профиль",
        "отчёт за неделю": "отчёт за неделю",
        "отчет за неделю": "отчет за неделю",
        "состояние банка": "состояние банка",
        "разбор моих рынков": "разбор моих рынков",
    }
    if norm in mapping:
        reply = await call_agent(user_id, mapping[norm])
        await update.message.reply_text(reply, reply_markup=MAIN_KB)
        return

    # --- всё остальное -> агент ---
    reply = await call_agent(user_id, text)

    # если создали ставку — покажем inline-кнопки результата
    m = re.search(r"Ставка сохранена \(id:\s*(\d+)\)", reply)
    if m:
        bet_id = int(m.group(1))
        await update.message.reply_text(reply, reply_markup=build_bet_result_keyboard(bet_id))
        await update.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    await update.message.reply_text(reply, reply_markup=MAIN_KB)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    await query.answer()

    # --- inline под матчем ---
    if data.startswith("MATCH_LINE:"):
        match_id = data.split(":", 1)[1]
        text = await call_agent(query.from_user.id, f"рынок {match_id} moneyline")
        await query.message.reply_text(text, reply_markup=MAIN_KB)
        return

    if data.startswith("MATCH_AI:"):
        match_id = data.split(":", 1)[1]
        text = await call_agent(query.from_user.id, f"аналитика {match_id} moneyline")
        await query.message.reply_text(text, reply_markup=MAIN_KB)
        return

    if data.startswith("MATCH_EXPERT:"):
        text = await call_agent(query.from_user.id, "стратегия")
        await query.message.reply_text(text, reply_markup=MAIN_KB)
        return

    # --- inline по ставке ---
    if data.startswith("BET_RES:"):
        _, bet_id_str, res = data.split(":")
        bet_id = int(bet_id_str)
        user_id = query.from_user.id

        mapping = {"win": "выигрыш", "lose": "проигрыш", "push": "возврат"}
        cmd = {
            "win": f"ставка {bet_id} выиграла",
            "lose": f"ставка {bet_id} проиграла",
            "push": f"ставка {bet_id} возврат",
        }[res]

        agent_reply = await call_agent(user_id, cmd)

        original = query.message.text or ""
        new_text = original + f"\n\n✅ Результат отмечен: {mapping[res]}."
        await query.edit_message_text(new_text)
        await query.message.reply_text(agent_reply, reply_markup=MAIN_KB)
        return


def build_telegram_application() -> Application:
    """
    ВАЖНО: используем это в webhook режиме (FastAPI).
    Не запускаем polling.
    """
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app
