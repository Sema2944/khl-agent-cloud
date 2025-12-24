# src/telegram_bot/bot.py
from __future__ import annotations

import os
import re
import logging
import asyncio
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

# -----------------------------
# Главное меню (минимально)
# -----------------------------
MAIN_KB = ReplyKeyboardMarkup(
    [
        ["🏟 Матчи сегодня"],
        ["🧠 AI Аналитика", "👤 Стратегия эксперта"],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)

# -----------------------------
# Inline клавиатуры (FSM через callback_data)
# -----------------------------
SPORTS = [
    ("hockey", "🏒 Хоккей"),
    ("football", "⚽ Футбол"),
    ("basketball", "🏀 Баскетбол"),
    ("tennis", "🎾 Теннис"),
    ("esports", "🎮 Киберспорт"),
]

MARKETS = [
    ("moneyline", "1X2 / Moneyline"),
    ("total", "Тотал"),
    ("handicap", "Фора"),
]


def kb_sports() -> InlineKeyboardMarkup:
    rows = []
    # 2 колонки
    buf = []
    for key, label in SPORTS:
        buf.append(InlineKeyboardButton(label, callback_data=f"SPORT:{key}"))
        if len(buf) == 2:
            rows.append(buf)
            buf = []
    if buf:
        rows.append(buf)
    return InlineKeyboardMarkup(rows)


def kb_matches(match_buttons: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    # 1 матч = 1 кнопка (чтобы не было тесно)
    rows = [[InlineKeyboardButton(title, callback_data=f"MATCH:{match_id}")]
            for match_id, title in match_buttons]
    # кнопка "назад"
    rows.append([InlineKeyboardButton("⬅️ Назад к спорту", callback_data="BACK:SPORTS")])
    return InlineKeyboardMarkup(rows)


def kb_markets(match_id: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(label, callback_data=f"MARKET:{match_id}:{key}")]
            for key, label in MARKETS]
    rows.append([InlineKeyboardButton("⬅️ Назад к матчам", callback_data="BACK:MATCHES")])
    return InlineKeyboardMarkup(rows)


def kb_market_actions(match_id: str, market_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📈 Рынок", callback_data=f"SHOW_MARKET:{match_id}:{market_key}"),
                InlineKeyboardButton("🧠 AI разбор", callback_data=f"AI:{match_id}:{market_key}"),
            ],
            [
                InlineKeyboardButton("👤 Мнение эксперта (если есть на сегодня)", callback_data="EXPERT_TODAY"),
            ],
            [
                InlineKeyboardButton("⬅️ Назад к рынкам", callback_data=f"BACK:MARKETS:{match_id}"),
            ],
        ]
    )


# -----------------------------
# Backend calls
# -----------------------------
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


# -----------------------------
# Парсинг матча из текста (из backend-ответа)
# Формат ожидаем:
# • СКА — ЦСКА (КХЛ) — id: `demo_hockey_001`
# -----------------------------
ID_RE = re.compile(r"id:\s*`?([a-zA-Z0-9_\-:.]{4,120})`?", re.IGNORECASE)


def extract_match_buttons(text: str) -> list[tuple[str, str]]:
    buttons: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        m = ID_RE.search(line)
        if not m:
            continue
        match_id = m.group(1).strip()
        # заголовок кнопки — строка без "— id: ..."
        title = re.sub(r"\s*—\s*id:\s*`?.+`?\s*$", "", line).strip()
        # уберём маркер списка
        title = title.lstrip("•").strip()
        if match_id and title:
            buttons.append((match_id, title))
    return buttons


# -----------------------------
# Нормализация текста кнопок меню
# -----------------------------
def _norm_menu(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[^\w\sа-яё-]", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# -----------------------------
# Handlers
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "✅ Я на связи.\n\nВыбирай действие кнопками ниже 👇",
        reply_markup=MAIN_KB,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    user_id = update.effective_user.id
    text = update.message.text or ""
    norm = _norm_menu(text)

    logger.info("handle_message user_id=%s text=%r", user_id, text)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # --- Главное меню: Матчи сегодня ---
    if norm in {"матчи сегодня"}:
        await update.message.reply_text(
            "Выбери спорт:",
            reply_markup=kb_sports(),
        )
        # отдельно вернём main kb, чтобы не пропадала
        await update.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # --- Главное меню: Стратегия эксперта ---
    if norm in {"стратегия эксперта", "стратегия", "эксперт", "эксперт сегодня"}:
        try:
            reply = await call_agent(user_id, "стратегия")
        except Exception:
            await update.message.reply_text("Backend недоступен 😔", reply_markup=MAIN_KB)
            return
        await update.message.reply_text(reply, reply_markup=MAIN_KB)
        return

    # --- Главное меню: AI аналитика (подсказка) ---
    if norm in {"ai аналитика", "аналитика", "ии аналитика"}:
        await update.message.reply_text(
            "Как пользоваться AI:\n"
            "1) нажми *🏟 Матчи сегодня*\n"
            "2) выбери спорт → матч → рынок\n"
            "3) нажми *🧠 AI разбор*\n\n"
            "Либо текстом: `аналитика <match_id> <market_key>`",
            reply_markup=MAIN_KB,
            parse_mode="Markdown",
        )
        return

    # --- Всё остальное отдаём агенту как есть (на случай ручных команд) ---
    try:
        reply = await call_agent(user_id, text)
    except Exception:
        await update.message.reply_text("Backend недоступен 😔\nПопробуй позже.", reply_markup=MAIN_KB)
        return

    await update.message.reply_text(reply, reply_markup=MAIN_KB)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    user_id = query.from_user.id
    await query.answer()

    # BACK
    if data == "BACK:SPORTS":
        await query.message.reply_text("Выбери спорт:", reply_markup=kb_sports())
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    if data == "BACK:MATCHES":
        # просим снова выбрать спорт (без хранения state)
        await query.message.reply_text("Выбери спорт:", reply_markup=kb_sports())
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    if data.startswith("BACK:MARKETS:"):
        # BACK:MARKETS:<match_id>
        match_id = data.split(":", 2)[2]
        await query.message.reply_text("Выбери рынок:", reply_markup=kb_markets(match_id))
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # SPORT -> список матчей
    if data.startswith("SPORT:"):
        sport_key = data.split(":", 1)[1]
        try:
            reply = await call_agent(user_id, f"матчи сегодня {sport_key}")
        except Exception:
            await query.message.reply_text("Backend недоступен 😔", reply_markup=MAIN_KB)
            return

        match_buttons = extract_match_buttons(reply)
        if not match_buttons:
            # покажем текст и вернём спорт
            await query.message.reply_text(reply)
            await query.message.reply_text("Выбери спорт:", reply_markup=kb_sports())
            await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
            return

        # покажем текст + кнопки матчей
        await query.message.reply_text(reply, parse_mode="Markdown")
        await query.message.reply_text("Выбери матч:", reply_markup=kb_matches(match_buttons))
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # MATCH -> экран матча (и рынок)
    if data.startswith("MATCH:"):
        match_id = data.split(":", 1)[1]
        try:
            reply = await call_agent(user_id, f"матч {match_id}")
        except Exception:
            await query.message.reply_text("Backend недоступен 😔", reply_markup=MAIN_KB)
            return

        await query.message.reply_text(reply, parse_mode="Markdown")
        await query.message.reply_text("Выбери рынок:", reply_markup=kb_markets(match_id))
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # MARKET -> действия (рынок/AI/эксперт)
    if data.startswith("MARKET:"):
        # MARKET:<match_id>:<market_key>
        _, match_id, market_key = data.split(":", 2)
        await query.message.reply_text(
            f"Выбран рынок: *{market_key}*\nВыбери действие:",
            reply_markup=kb_market_actions(match_id, market_key),
            parse_mode="Markdown",
        )
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # SHOW_MARKET -> /рынок
    if data.startswith("SHOW_MARKET:"):
        _, match_id, market_key = data.split(":", 2)
        try:
            reply = await call_agent(user_id, f"рынок {match_id} {market_key}")
        except Exception:
            reply = "Backend недоступен 😔"

        await query.message.reply_text(reply, parse_mode="Markdown")
        # оставить действия под рукой
        await query.message.reply_text(
            "Действия:",
            reply_markup=kb_market_actions(match_id, market_key),
        )
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # AI -> /аналитика
    if data.startswith("AI:"):
        _, match_id, market_key = data.split(":", 2)
        try:
            reply = await call_agent(user_id, f"аналитика {match_id} {market_key}")
        except Exception:
            reply = "Backend недоступен 😔"

        await query.message.reply_text(reply, parse_mode="Markdown")
        await query.message.reply_text(
            "Действия:",
            reply_markup=kb_market_actions(match_id, market_key),
        )
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # EXPERT_TODAY -> стратегия на сегодня
    if data == "EXPERT_TODAY":
        try:
            reply = await call_agent(user_id, "стратегия")
        except Exception:
            reply = "Backend недоступен 😔"

        await query.message.reply_text(reply, parse_mode="Markdown")
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # неизвестное callback
    await query.message.reply_text("Не понял действие 🤔", reply_markup=MAIN_KB)


# -----------------------------
# Application builder (полезно для webhook-интеграции)
# -----------------------------
def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app


# -----------------------------
# Polling main (если запускаешь отдельным сервисом)
# -----------------------------
def main() -> None:
    logger.info("Starting Telegram bot (polling). API_BASE=%r TIMEOUT=%s", API_BASE, BACKEND_TIMEOUT)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = build_application()
    app.run_polling(stop_signals=None)


if __name__ == "__main__":
    main()
