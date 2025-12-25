# src/telegram_bot/app.py
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, FastAPI, Request
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").strip().rstrip("/")  # e.g. https://khl-agent-api-9dw6.onrender.com
WEBHOOK_PATH = (os.getenv("TELEGRAM_WEBHOOK_PATH") or "/telegram/webhook").strip()
WEBHOOK_SECRET = (os.getenv("TELEGRAM_WEBHOOK_SECRET") or "").strip()  # optional
API_BASE = (os.getenv("API_BASE") or "").strip().rstrip("/")  # backend base (can be same service)
BACKEND_TIMEOUT = float((os.getenv("BACKEND_TIMEOUT") or "8").strip())

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

# Если API_BASE не задан — будем пытаться использовать этот же сервис (PUBLIC_URL)
if not API_BASE and PUBLIC_URL:
    API_BASE = PUBLIC_URL

# -----------------------------
# UI / DATA (demo)
# -----------------------------
SPORTS: list[tuple[str, str]] = [
    ("hockey", "🏒 Хоккей"),
    ("football", "⚽ Футбол"),
    ("basketball", "🏀 Баскетбол"),
    ("tennis", "🎾 Теннис"),
    ("esports", "🎮 Киберспорт"),
]

MARKETS: list[tuple[str, str]] = [
    ("moneyline", "🏁 1X2 / Moneyline"),
    ("total", "📊 Total"),
    ("handicap", "📐 Handicap"),
]


# -----------------------------
# HTTP helper
# -----------------------------
async def _safe_request(method: str, url: str, **kwargs) -> Dict[str, Any]:
    timeout = kwargs.pop("timeout", BACKEND_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.request(method, url, **kwargs)
        r.raise_for_status()
        return r.json()


async def call_agent(user_id: int, message: str) -> str:
    if not API_BASE:
        return "Backend не настроен (API_BASE/PUBLIC_URL пуст)."
    payload = {"user_id": user_id, "message": message}
    data = await _safe_request("POST", f"{API_BASE}/agent/query", json=payload, timeout=BACKEND_TIMEOUT)
    return str(data.get("reply") or "Пустой ответ от сервера 😕")


# -----------------------------
# Keyboards
# -----------------------------
def kb_sports() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(title, callback_data=f"SPORT:{key}")] for key, title in SPORTS]
    return InlineKeyboardMarkup(rows)


def kb_matches_from_text(text: str) -> Optional[InlineKeyboardMarkup]:
    """
    Парсим reply backend вида:
    • Команда — Команда (...) — id: `demo_xxx_001`
    """
    ids = re.findall(r"id:\s*`([^`]+)`", text)
    if not ids:
        return None

    # для красоты: вытаскиваем строку матча до "— id:"
    lines = text.splitlines()
    buttons: list[list[InlineKeyboardButton]] = []
    for match_id in ids:
        label = match_id
        for ln in lines:
            if f"`{match_id}`" in ln:
                # "• Зенит — Спартак (РПЛ) — id: `demo_football_001`"
                label = ln.strip().lstrip("•").strip()
                label = re.sub(r"\s*—\s*id:\s*`[^`]+`", "", label).strip()
                break
        buttons.append([InlineKeyboardButton(label, callback_data=f"MATCH:{match_id}")])

    # нижний ряд: назад к спорту
    buttons.append([InlineKeyboardButton("⬅️ Назад к спорту", callback_data="BACK:SPORTS")])
    return InlineKeyboardMarkup(buttons)


def kb_markets(match_id: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(title, callback_data=f"MARKET:{match_id}:{key}")] for key, title in MARKETS]
    rows.append([InlineKeyboardButton("⬅️ Назад к матчам", callback_data="BACK:MATCHES")])
    return InlineKeyboardMarkup(rows)


def kb_market_actions(match_id: str, market_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📈 Линия", callback_data=f"LINE:{match_id}:{market_key}"),
                InlineKeyboardButton("🧠 AI разбор", callback_data=f"AI:{match_id}:{market_key}"),
            ],
            [InlineKeyboardButton("👤 Мнение эксперта", callback_data=f"EXPERT:{match_id}:{market_key}")],
            [InlineKeyboardButton("⬅️ Назад к рынкам", callback_data=f"BACK:MARKETS:{match_id}")],
        ]
    )


# -----------------------------
# Telegram handlers
# -----------------------------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # Главное действие: выводим спорт-меню inline
    norm = text.lower()
    if "матчи" in norm:
        context.user_data.pop("last_sport", None)
        context.user_data.pop("last_matches_text", None)
        await update.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
        return

    if "стратег" in norm or norm in {"эксперт", "стратегия"}:
        reply = await call_agent(user_id, "стратегия")
        await update.message.reply_text(reply)
        return

    if "аналитик" in norm:
        await update.message.reply_text(
            "🧠 AI Аналитика\n\n"
            "Лучший путь: Матчи сегодня → матч → рынок → AI разбор.\n"
            "Или командой: `аналитика <match_id> <market_key>`"
        )
        return

    # Фолбек: всё в агента
    reply = await call_agent(user_id, text)
    await update.message.reply_text(reply)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user_id = query.from_user.id
    data = query.data or ""

    # BACK
    if data == "BACK:SPORTS":
        await query.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
        return

    if data == "BACK:MATCHES":
        # восстановим последний спорт
        sport = context.user_data.get("last_sport")
        if not sport:
            await query.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
            return
        await _send_matches_for_sport(query, context, sport)
        return

    if data.startswith("BACK:MARKETS:"):
        match_id = data.split(":", 2)[2]
        await query.message.reply_text("📊 Выбери рынок:", reply_markup=kb_markets(match_id))
        return

    # SPORT choose
    if data.startswith("SPORT:"):
        sport = data.split(":", 1)[1]
        context.user_data["last_sport"] = sport
        await _send_matches_for_sport(query, context, sport)
        return

    # MATCH choose
    if data.startswith("MATCH:"):
        match_id = data.split(":", 1)[1]
        context.user_data["last_match"] = match_id
        await query.message.reply_text("📊 Выбери рынок:", reply_markup=kb_markets(match_id))
        return

    # MARKET choose
    if data.startswith("MARKET:"):
        _, match_id, market_key = data.split(":", 2)
        context.user_data["last_match"] = match_id
        context.user_data["last_market"] = market_key
        await query.message.reply_text(
            f"✅ Выбран рынок: `{market_key}`\nМатч: `{match_id}`",
            reply_markup=kb_market_actions(match_id, market_key),
        )
        return

    # ACTIONS
    if data.startswith("LINE:"):
        _, match_id, market_key = data.split(":", 2)
        text = await call_agent(user_id, f"рынок {match_id} {market_key}")
        await query.message.reply_text(text, reply_markup=kb_market_actions(match_id, market_key))
        return

    if data.startswith("AI:"):
        _, match_id, market_key = data.split(":", 2)
        text = await call_agent(user_id, f"аналитика {match_id} {market_key}")
        await query.message.reply_text(text, reply_markup=kb_market_actions(match_id, market_key))
        return

    if data.startswith("EXPERT:"):
        _, match_id, market_key = data.split(":", 2)
        text = await call_agent(user_id, "стратегия")
        await query.message.reply_text(text, reply_markup=kb_market_actions(match_id, market_key))
        return


async def _send_matches_for_sport(query, context: ContextTypes.DEFAULT_TYPE, sport: str) -> None:
    await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.TYPING)
    reply = await call_agent(query.from_user.id, f"матчи сегодня {sport}")
    context.user_data["last_matches_text"] = reply

    kb = kb_matches_from_text(reply)
    if kb:
        await query.message.reply_text(reply, reply_markup=kb)
    else:
        await query.message.reply_text(
            reply + "\n\nНе смог построить кнопки матчей — проверь формат `id: `...``"
        )


# -----------------------------
# Build Application (required by service.py)
# -----------------------------
def build_telegram_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


# -----------------------------
# FastAPI mount
# -----------------------------
def mount(fastapi_app: FastAPI) -> None:
    """
    Регистрирует /telegram/webhook и на startup ставит webhook.
    """
    tg_app = build_telegram_application()
    router = APIRouter()

    @router.post(WEBHOOK_PATH)
    async def telegram_webhook(req: Request):
        # optional secret in header
        if WEBHOOK_SECRET:
            got = req.headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
            if got != WEBHOOK_SECRET:
                return {"ok": False, "error": "bad secret"}

        payload = await req.json()
        update = Update.de_json(payload, tg_app.bot)
        await tg_app.process_update(update)
        return {"ok": True}

    fastapi_app.include_router(router)

    @fastapi_app.on_event("startup")
    async def _startup_webhook():
        if not PUBLIC_URL:
            logger.warning("PUBLIC_URL is not set -> webhook will NOT be configured automatically.")
            return

        webhook_url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
        try:
            await tg_app.bot.set_webhook(
                url=webhook_url,
                secret_token=WEBHOOK_SECRET or None,
                drop_pending_updates=True,
            )
            logger.info("Telegram webhook set: %s", webhook_url)
        except Exception as e:
            logger.exception("Failed to set telegram webhook: %s", e)

    @fastapi_app.on_event("shutdown")
    async def _shutdown():
        try:
            await tg_app.bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            pass
