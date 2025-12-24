# src/telegram_bot/app.py
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

import httpx
from fastapi import APIRouter, FastAPI, Request
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

# -----------------------------
# ENV
# -----------------------------
BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
API_BASE = (os.getenv("API_BASE") or "").strip().rstrip("/")
BACKEND_TIMEOUT = float((os.getenv("BACKEND_TIMEOUT") or "8").strip())
WEBHOOK_SECRET = (os.getenv("TELEGRAM_WEBHOOK_SECRET") or "").strip()  # optional

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

if not API_BASE:
    logger.warning("API_BASE is not set. Bot will work in 'no-backend' mode.")

# -----------------------------
# FastAPI router (mounted in src/service.py)
# -----------------------------
router = APIRouter()

# -----------------------------
# Reply keyboard (одно меню, без дублей)
# -----------------------------
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

# -----------------------------
# Inline keyboards
# -----------------------------
SPORTS = [
    ("hockey", "🏒 Хоккей"),
    ("football", "⚽ Футбол"),
    ("basketball", "🏀 Баскетбол"),
    ("tennis", "🎾 Теннис"),
    ("esports", "🎮 Киберспорт"),
]


def kb_sports() -> InlineKeyboardMarkup:
    rows = []
    for key, title in SPORTS:
        rows.append([InlineKeyboardButton(title, callback_data=f"SPORT:{key}")])
    return InlineKeyboardMarkup(rows)


def kb_match_actions(match_id: str) -> InlineKeyboardMarkup:
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


def kb_match_markets(match_id: str) -> InlineKeyboardMarkup:
    # market_key должен совпасть с parsing.py DEMO_MARKETS keys
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💰 Moneyline", callback_data=f"MARKET:{match_id}:moneyline"),
                InlineKeyboardButton("🎯 Тотал", callback_data=f"MARKET:{match_id}:total"),
            ],
            [
                InlineKeyboardButton("➗ Фора", callback_data=f"MARKET:{match_id}:handicap"),
            ],
            [
                InlineKeyboardButton("⬅️ Назад к матчу", callback_data=f"MATCH:{match_id}"),
            ],
        ]
    )


def kb_market_actions(match_id: str, market_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📈 Показать рынок", callback_data=f"SHOW_MARKET:{match_id}:{market_key}"
                ),
                InlineKeyboardButton(
                    "🧠 AI аналитика", callback_data=f"AI:{match_id}:{market_key}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "👤 Мнение эксперта", callback_data=f"EXPERT:{match_id}:{market_key}"
                )
            ],
            [
                InlineKeyboardButton("⬅️ К рынкам", callback_data=f"MATCH:{match_id}"),
            ],
        ]
    )


def kb_bet_result(bet_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🟢 Выиграла", callback_data=f"BET_RES:{bet_id}:win"),
                InlineKeyboardButton("🔴 Проиграла", callback_data=f"BET_RES:{bet_id}:lose"),
            ],
            [InlineKeyboardButton("⚪️ Возврат", callback_data=f"BET_RES:{bet_id}:push")],
        ]
    )


# -----------------------------
# Backend helpers
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


def _normalize_menu_text(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[^\w\sа-яё-]", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _extract_first_match_id(text: str) -> Optional[str]:
    # Ожидаем формат "... id: `demo_hockey_001`" или "... id: demo_hockey_001"
    m = re.search(r"id:\s*`?([a-zA-Z0-9_\-:.]{4,80})`?", text)
    return m.group(1) if m else None


# -----------------------------
# Telegram handlers (webhook mode)
# -----------------------------
async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    user_id = update.effective_user.id
    text = update.message.text or ""
    norm = _normalize_menu_text(text)

    logger.info("handle_message user_id=%s text=%r", user_id, text)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # Главное: всегда держим меню
    async def reply_menu(msg: str) -> None:
        await update.message.reply_text(msg, reply_markup=MAIN_KB)

    # --- UX входы ---
    if norm in {"матчи сегодня", "матчи", "сегодня матчи"}:
        # Сначала выбор спорта (inline)
        await update.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
        # И сразу же меню (чтобы не пропадало)
        await update.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    if norm in {"ai аналитика", "аналитика", "ии аналитика"}:
        await reply_menu(
            "🧠 AI Аналитика\n\n"
            "Сценарий:\n"
            "1) 🏟 Матчи сегодня → выбрать спорт\n"
            "2) выбрать матч → выбрать рынок\n"
            "3) нажать 🧠 AI аналитика\n\n"
            "Либо текстом: `аналитика <match_id> <market_key>`"
        )
        return

    if norm in {"стратегия эксперта", "стратегия", "эксперт", "эксперт сегодня"}:
        await reply_menu(await call_agent(user_id, "стратегия"))
        return

    if norm in {"профиль"}:
        await reply_menu(await call_agent(user_id, "профиль"))
        return

    if norm in {"отчёт за неделю", "отчет за неделю"}:
        await reply_menu(await call_agent(user_id, "отчёт за неделю"))
        return

    if norm in {"состояние банка"}:
        await reply_menu(await call_agent(user_id, "состояние банка"))
        return

    if norm in {"разбор моих рынков"}:
        await reply_menu(await call_agent(user_id, "разбор моих рынков"))
        return

    if norm in {"мои ставки"}:
        bets = await call_last_bets(user_id, 5)
        if not bets:
            await reply_menu("Ставок нет.")
            return
        await update.message.reply_text("Твои последние ставки:", reply_markup=MAIN_KB)
        for b in bets:
            bet_id = b.get("id")
            result = b.get("result")
            created_at = b.get("created_at", "")
            event = b.get("event", "")
            outcome = b.get("outcome", "")
            stake = b.get("stake")
            odds = b.get("odds")
            profit = b.get("profit")

            lines = [f"Ставка #{bet_id}"]
            if created_at:
                lines.append(f"Дата: {created_at}")
            if event:
                lines.append(f"Событие: {event}")
            if outcome:
                lines.append(f"Исход: {outcome}")
            if stake is not None:
                lines.append(f"Сумма: {stake}")
            if odds is not None:
                lines.append(f"Коэффициент: {odds}")
            if result:
                lines.append(f"Результат: {result} (PnL: {profit})")

            msg = "\n".join(lines)
            if (result is None) and (bet_id is not None):
                await update.message.reply_text(msg, reply_markup=kb_bet_result(int(bet_id)))
            else:
                await update.message.reply_text(msg, reply_markup=MAIN_KB)
        return

    # Всё остальное — в агента как есть
    reply = await call_agent(user_id, text)

    # Если агент вернул список матчей — можно сразу дать подсказку
    await reply_menu(reply)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()

    user_id = q.from_user.id
    data = q.data or ""

    async def send(msg: str, *, kb=None):
        await q.message.reply_text(msg, reply_markup=(kb or MAIN_KB))

    # --- Выбор спорта -> показываем матчи сегодня ---
    if data.startswith("SPORT:"):
        sport = data.split(":", 1)[1]
        text = await call_agent(user_id, f"матчи сегодня {sport}")
        # Пытаемся вытащить первый match_id для действий (но лучше — пусть пользователь выберет матч)
        await send(text, kb=MAIN_KB)
        await send("Выбери матч: нажми на id в сообщении и отправь команду `матч <id>`.\n"
                   "Или просто скопируй id: например `матч demo_hockey_001`.", kb=MAIN_KB)
        return

    # --- Экран матча: показать рынки ---
    if data.startswith("MATCH:"):
        match_id = data.split(":", 1)[1]
        text = await call_agent(user_id, f"матч {match_id}")
        await send(text, kb=kb_match_markets(match_id))
        return

    # --- Из списка матчей: линия/ai/expert (быстрые кнопки) ---
    if data.startswith("MATCH_LINE:"):
        match_id = data.split(":", 1)[1]
        # MVP: покажем рынок moneyline по умолчанию
        text = await call_agent(user_id, f"рынок {match_id} moneyline")
        await send(text, kb=kb_market_actions(match_id, "moneyline"))
        return

    if data.startswith("MATCH_AI:"):
        match_id = data.split(":", 1)[1]
        text = await call_agent(user_id, f"аналитика {match_id} moneyline")
        await send(text, kb=kb_market_actions(match_id, "moneyline"))
        return

    if data.startswith("MATCH_EXPERT:"):
        match_id = data.split(":", 1)[1]
        text = await call_agent(user_id, "стратегия")
        await send(text, kb=kb_match_actions(match_id))
        return

    # --- Выбор рынка ---
    if data.startswith("MARKET:"):
        _, match_id, market_key = data.split(":")
        # покажем рынок (как экран), и действия
        text = await call_agent(user_id, f"рынок {match_id} {market_key}")
        await send(text, kb=kb_market_actions(match_id, market_key))
        return

    if data.startswith("SHOW_MARKET:"):
        _, match_id, market_key = data.split(":")
        text = await call_agent(user_id, f"рынок {match_id} {market_key}")
        await send(text, kb=kb_market_actions(match_id, market_key))
        return

    if data.startswith("AI:"):
        _, match_id, market_key = data.split(":")
        text = await call_agent(user_id, f"аналитика {match_id} {market_key}")
        await send(text, kb=kb_market_actions(match_id, market_key))
        return

    if data.startswith("EXPERT:"):
        # MVP: просто стратегия дня
        text = await call_agent(user_id, "стратегия")
        await send(text, kb=MAIN_KB)
        return

    # --- Результат ставки ---
    if data.startswith("BET_RES:"):
        _, bet_id_str, res = data.split(":")
        bet_id = int(bet_id_str)

        cmd = {
            "win": f"ставка {bet_id} выиграла",
            "lose": f"ставка {bet_id} проиграла",
            "push": f"ставка {bet_id} возврат",
        }[res]
        agent_reply = await call_agent(user_id, cmd)

        original = q.message.text or ""
        await q.edit_message_text(original + f"\n\n✅ Результат отмечен: {res}.")
        await send(agent_reply, kb=MAIN_KB)
        return

    await send("Неизвестное действие 😕", kb=MAIN_KB)


# -----------------------------
# App singleton (Telegram Application)
# -----------------------------
@dataclass
class _State:
    app: Optional[Application] = None


_state = _State()


async def get_tg_app() -> Application:
    if _state.app is not None:
        return _state.app

    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CallbackQueryHandler(on_callback))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))
    await tg_app.initialize()
    await tg_app.start()
    _state.app = tg_app
    logger.info("Telegram Application initialized for webhook mode.")
    return tg_app


# -----------------------------
# Webhook endpoint
# -----------------------------
@router.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    # Optional: simple secret header check (если хочешь защиту)
    if WEBHOOK_SECRET:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
        if got != WEBHOOK_SECRET:
            logger.warning("Bad webhook secret token")
            return {"ok": False}

    payload = await request.json()
    tg_app = await get_tg_app()

    update = Update.de_json(payload, tg_app.bot)
    await tg_app.process_update(update)

    return {"ok": True}


# -----------------------------
# Helper to mount into FastAPI
# -----------------------------
def mount(app: FastAPI) -> None:
    app.include_router(router)
