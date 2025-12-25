# src/telegram_bot/app.py
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

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
    rows = [[InlineKeyboardButton(title, callback_data=f"SPORT:{key}")]] for key, title in SPORTS
    return InlineKeyboardMarkup(rows)


def kb_matches(matches: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    """
    Каждая кнопка -> MATCH:<match_id>
    """
    rows = []
    for m in matches:
        title = m.get("title") or "Матч"
        league = m.get("league") or ""
        label = f"{title} ({league})" if league else title
        rows.append([InlineKeyboardButton(label, callback_data=f"MATCH:{m['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Назад к спорту", callback_data="BACK:SPORTS")])
    return InlineKeyboardMarkup(rows)


def kb_match_actions(match_id: str) -> InlineKeyboardMarkup:
    """
    Под матчем: выбор рынка + AI по матчу
    """
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🧠 AI аналитика по матчу", callback_data=f"AI_MATCH:{match_id}")],
            [
                InlineKeyboardButton("💰 Moneyline", callback_data=f"MARKET:{match_id}:moneyline"),
                InlineKeyboardButton("🎯 Total", callback_data=f"MARKET:{match_id}:total"),
            ],
            [InlineKeyboardButton("➗ Handicap", callback_data=f"MARKET:{match_id}:handicap")],
            [InlineKeyboardButton("👤 Мнение эксперта", callback_data=f"EXPERT_MATCH:{match_id}")],
            [InlineKeyboardButton("⬅️ К списку матчей", callback_data="BACK:MATCHES")],
        ]
    )


def kb_market_actions(match_id: str, market_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📈 Показать рынок", callback_data=f"SHOW_MARKET:{match_id}:{market_key}"),
                InlineKeyboardButton("🧠 AI по рынку", callback_data=f"AI:{match_id}:{market_key}"),
            ],
            [InlineKeyboardButton("👤 Мнение эксперта", callback_data=f"EXPERT:{match_id}:{market_key}")],
            [InlineKeyboardButton("⬅️ К матчу", callback_data=f"MATCH:{match_id}")],
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


def _extract_demo_matches_from_text(reply: str) -> List[Dict[str, Any]]:
    """
    Парсим из ответа агента строки вида:
    • Зенит — Спартак (РПЛ) — id: `demo_football_001`
    """
    matches: List[Dict[str, Any]] = []
    if not reply:
        return matches

    # Быстро и устойчиво под твой формат
    for line in reply.splitlines():
        line = line.strip()
        if "— id:" not in line:
            continue

        # title (league) — id: `xxx`
        m = re.search(r"•\s*(.+?)\s*\((.+?)\)\s*—\s*id:\s*`([^`]+)`", line)
        if m:
            title = m.group(1).strip()
            league = m.group(2).strip()
            mid = m.group(3).strip()
            matches.append({"id": mid, "title": title, "league": league})
            continue

        # fallback: id: `xxx` + всё до него как title
        m2 = re.search(r"id:\s*`([^`]+)`", line)
        if m2:
            mid = m2.group(1).strip()
            title = re.sub(r".*•\s*", "", line)
            title = re.sub(r"\s*—\s*id:\s*`[^`]+`", "", title).strip()
            matches.append({"id": mid, "title": title, "league": ""})

    return matches


# -----------------------------
# In-memory user state (минимально)
# -----------------------------
@dataclass
class UserNavState:
    last_sport: Optional[str] = None
    last_matches: Optional[List[Dict[str, Any]]] = None


_STATE_BY_USER: dict[int, UserNavState] = {}


def _st(user_id: int) -> UserNavState:
    if user_id not in _STATE_BY_USER:
        _STATE_BY_USER[user_id] = UserNavState()
    return _STATE_BY_USER[user_id]


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

    async def reply_menu(msg: str) -> None:
        await update.message.reply_text(msg, reply_markup=MAIN_KB)

    if norm in {"матчи сегодня", "матчи", "сегодня матчи"}:
        await update.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
        await update.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    if norm in {"ai аналитика", "аналитика", "ии аналитика"}:
        await reply_menu(
            "🧠 AI Аналитика\n\n"
            "Сценарий:\n"
            "1) 🏟 Матчи сегодня → выбрать спорт\n"
            "2) выбрать матч → выбрать рынок\n"
            "3) нажать 🧠 AI\n\n"
            "Либо текстом: `аналитика <match_id> <market_key>`"
        )
        return

    if norm in {"стратегия эксперта", "стратегия", "эксперт", "эксперт сегодня"}:
        await reply_menu(await call_agent(user_id, "стратегия"))
        return

    if norm == "профиль":
        await reply_menu(await call_agent(user_id, "профиль"))
        return

    if norm in {"отчёт за неделю", "отчет за неделю"}:
        await reply_menu(await call_agent(user_id, "отчёт за неделю"))
        return

    if norm == "состояние банка":
        await reply_menu(await call_agent(user_id, "состояние банка"))
        return

    if norm == "разбор моих рынков":
        await reply_menu(await call_agent(user_id, "разбор моих рынков"))
        return

    if norm == "мои ставки":
        bets = await call_last_bets(user_id, 5)
        if not bets:
            await reply_menu("Ставок нет.")
            return

        await update.message.reply_text("Твои последние ставки:", reply_markup=MAIN_KB)
        for b in bets:
            bet_id = b.get("id")
            result = b.get("result")

            lines = [f"Ставка #{bet_id}"]
            if b.get("event"):
                lines.append(f"Событие: {b.get('event')}")
            if b.get("outcome"):
                lines.append(f"Исход: {b.get('outcome')}")
            if b.get("stake") is not None:
                lines.append(f"Сумма: {b.get('stake')}")
            if b.get("odds") is not None:
                lines.append(f"Коэффициент: {b.get('odds')}")
            if result:
                lines.append(f"Результат: {result} (PnL: {b.get('profit')})")

            msg = "\n".join(lines)
            if (result is None) and (bet_id is not None):
                await update.message.reply_text(msg, reply_markup=kb_bet_result(int(bet_id)))
            else:
                await update.message.reply_text(msg, reply_markup=MAIN_KB)
        return

    # Остальное — в агента
    reply = await call_agent(user_id, text)
    await reply_menu(reply)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()

    user_id = q.from_user.id
    data = q.data or ""
    state = _st(user_id)

    async def send(msg: str, kb=None):
        await q.message.reply_text(msg, reply_markup=(kb or MAIN_KB))

    # --- NAV ---
    if data == "BACK:SPORTS":
        await send("🏟 Выбери спорт:", kb=kb_sports())
        return

    if data == "BACK:MATCHES":
        if state.last_matches:
            await send("🏟 Выбери матч:", kb=kb_matches(state.last_matches))
        else:
            await send("🏟 Выбери спорт:", kb=kb_sports())
        return

    # --- SPORT -> show matches as BUTTONS ---
    if data.startswith("SPORT:"):
        sport = data.split(":", 1)[1].strip()
        state.last_sport = sport

        text = await call_agent(user_id, f"матчи сегодня {sport}")
        matches = _extract_demo_matches_from_text(text)
        state.last_matches = matches if matches else None

        await send(text, kb=MAIN_KB)

        if matches:
            await send("🏟 Выбери матч:", kb=kb_matches(matches))
        else:
            await send("Не смог построить кнопки матчей (нет id в ответе).", kb=MAIN_KB)
        return

    # --- MATCH -> show match screen + inline markets + AI ---
    if data.startswith("MATCH:"):
        match_id = data.split(":", 1)[1].strip()
        text = await call_agent(user_id, f"матч {match_id}")
        await send(text, kb=kb_match_actions(match_id))
        return

    # --- AI match (общая аналитика по матчу, без рынка) ---
    if data.startswith("AI_MATCH:"):
        match_id = data.split(":", 1)[1].strip()
        # MVP: если в parsing.py нет спец-команды, можно направить на moneyline или общий режим
        text = await call_agent(user_id, f"аналитика {match_id} moneyline")
        await send(text, kb=kb_match_actions(match_id))
        return

    # --- expert match (пока стратегия дня) ---
    if data.startswith("EXPERT_MATCH:"):
        text = await call_agent(user_id, "стратегия")
        await send(text, kb=MAIN_KB)
        return

    # --- MARKET -> show market + actions ---
    if data.startswith("MARKET:"):
        _, match_id, market_key = data.split(":")
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
        text = await call_agent(user_id, "стратегия")
        await send(text, kb=MAIN_KB)
        return

    # --- BET result ---
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
# Telegram Application singleton
# -----------------------------
@dataclass
class _State:
    app: Optional[Application] = None


_state = _State()


async def build_telegram_application() -> Application:
    """
    service.py импортирует build_telegram_application.
    В webhook-режиме НЕ запускаем polling.
    """
    if _state.app is not None:
        return _state.app

    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CallbackQueryHandler(on_callback))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))

    await tg_app.initialize()
    await tg_app.start()

    _state.app = tg_app
    logger.info("Telegram Application initialized (webhook mode).")
    return tg_app


# -----------------------------
# Webhook endpoint
# -----------------------------
@router.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if WEBHOOK_SECRET:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
        if got != WEBHOOK_SECRET:
            logger.warning("Bad webhook secret token")
            return {"ok": False}

    payload = await request.json()
    tg_app = await build_telegram_application()

    update = Update.de_json(payload, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}


def mount(app: FastAPI) -> None:
    app.include_router(router)
