# src/telegram_bot/app.py
from __future__ import annotations

import logging
import os
import re
from typing import Any, List, Tuple

from fastapi import FastAPI, Request, HTTPException
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logger = logging.getLogger(__name__)

# =========================================================
# ENV
# =========================================================
BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").strip().rstrip("/")
WEBHOOK_PATH = (os.getenv("TELEGRAM_WEBHOOK_PATH") or "/telegram/webhook").strip()
WEBHOOK_SECRET = (os.getenv("TELEGRAM_WEBHOOK_SECRET") or "").strip()

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

# =========================================================
# SAFE TEXT (убираем Markdown-краши)
# =========================================================
def safe_text(text: str) -> str:
    if not text:
        return ""
    # Telegram часто падает из-за `_ * [ ] ( )`
    return re.sub(r"([_*[\]()~`>#+=|{}.!])", r"\\\1", text)

# =========================================================
# MAIN MENU
# =========================================================
MAIN_KB = ReplyKeyboardMarkup(
    [
        ["🏟 Матчи сегодня"],
        ["🧠 AI Аналитика", "👤 Стратегия эксперта"],
        ["📊 Профиль"],
    ],
    resize_keyboard=True,
)

# =========================================================
# SPORTS
# =========================================================
SPORTS = [
    ("hockey", "🏒 Хоккей"),
    ("football", "⚽ Футбол"),
    ("basketball", "🏀 Баскетбол"),
    ("tennis", "🎾 Теннис"),
    ("esports", "🎮 Киберспорт"),
]

# =========================================================
# HELPERS
# =========================================================
ID_RE = re.compile(r"id:\s*([a-zA-Z0-9_\-:.]{4,120})", re.IGNORECASE)

def extract_match_buttons(text: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for line in (text or "").splitlines():
        m = ID_RE.search(line)
        if not m:
            continue
        match_id = m.group(1)
        title = re.sub(r"\s*—\s*id:.*$", "", line).lstrip("•").strip()
        if title and match_id:
            out.append((match_id, title))
    return out

# =========================================================
# INLINE KEYBOARDS
# =========================================================
def kb_sports() -> InlineKeyboardMarkup:
    rows = []
    buf = []
    for key, label in SPORTS:
        buf.append(InlineKeyboardButton(label, callback_data=f"SPORT:{key}"))
        if len(buf) == 2:
            rows.append(buf)
            buf = []
    if buf:
        rows.append(buf)
    return InlineKeyboardMarkup(rows)

def kb_matches(items: List[Tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(title, callback_data=f"MATCH:{mid}")]
            for mid, title in items]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="BACK:SPORTS")])
    return InlineKeyboardMarkup(rows)

def kb_match_hub(match_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Обзор", callback_data=f"UI:{match_id}:pre:overview")],
            [
                InlineKeyboardButton("🧠 1X2", callback_data=f"UI:{match_id}:pre:moneyline"),
                InlineKeyboardButton("🧠 Тотал", callback_data=f"UI:{match_id}:pre:total"),
            ],
            [InlineKeyboardButton("🧠 Фора", callback_data=f"UI:{match_id}:pre:handicap")],
            [
                InlineKeyboardButton("🟢 LIVE", callback_data=f"UI:{match_id}:live:overview"),
                InlineKeyboardButton("🔄 Обновить", callback_data=f"UI:{match_id}:live:refresh"),
            ],
            [InlineKeyboardButton("⬅️ К матчам", callback_data="BACK:MATCHES")],
        ]
    )

# =========================================================
# LOCAL AGENT
# =========================================================
async def call_agent_local(user_id: int, message: str) -> str:
    from ..parsing import run_dialog_agent
    return await run_dialog_agent(user_id=user_id, message=message)

# =========================================================
# HANDLERS
# =========================================================
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
    norm = text.lower().strip()

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    # --- Матчи сегодня ---
    if norm == "🏟 матчи сегодня" or norm == "матчи сегодня":
        await update.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
        await update.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # --- Профиль ---
    if "профиль" in norm:
        reply = await call_agent_local(user_id, "профиль")
        await update.message.reply_text(safe_text(reply), reply_markup=MAIN_KB)
        return

    # --- Стратегия ---
    if "стратегия" in norm or "эксперт" in norm:
        reply = await call_agent_local(user_id, "стратегия")
        await update.message.reply_text(safe_text(reply), reply_markup=MAIN_KB)
        return

    # --- AI help ---
    if "ai" in norm or "аналитика" in norm:
        await update.message.reply_text(
            "Как пользоваться:\n"
            "1) 🏟 Матчи сегодня\n"
            "2) спорт → матч\n"
            "3) 📊 Обзор / 🧠 рынки\n"
            "4) 🟢 LIVE\n\n"
            "Диагностика: llm ping / env / version",
            reply_markup=MAIN_KB,
        )
        return

    # --- default ---
    reply = await call_agent_local(user_id, text)
    await update.message.reply_text(safe_text(reply), reply_markup=MAIN_KB)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()
    user_id = query.from_user.id
    data = query.data or ""

    await context.bot.send_chat_action(query.message.chat_id, ChatAction.TYPING)

    # BACK -> SPORTS
    if data == "BACK:SPORTS":
        await query.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # BACK -> MATCHES
    if data == "BACK:MATCHES":
        items = context.user_data.get("last_match_buttons") or []
        if items:
            await query.message.reply_text("Выбери матч:", reply_markup=kb_matches(items))
        else:
            await query.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # SPORT
    if data.startswith("SPORT:"):
        sport = data.split(":", 1)[1]
        reply = await call_agent_local(user_id, f"матчи сегодня {sport}")
        items = extract_match_buttons(reply)
        context.user_data["last_match_buttons"] = items

        await query.message.reply_text(safe_text(reply))
        if items:
            await query.message.reply_text("Выбери матч:", reply_markup=kb_matches(items))
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # MATCH
    if data.startswith("MATCH:"):
        match_id = data.split(":", 1)[1]
        reply = await call_agent_local(user_id, f"матч {match_id}")
        await query.message.reply_text(safe_text(reply))
        await query.message.reply_text("Действия:", reply_markup=kb_match_hub(match_id))
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # UI
    if data.startswith("UI:"):
        _, match_id, mode, action = data.split(":")
        reply = await call_agent_local(user_id, f"ui match {match_id} {mode} {action}")
        await query.message.reply_text(safe_text(reply))
        await query.message.reply_text("Действия:", reply_markup=kb_match_hub(match_id))
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    await query.message.reply_text("Не понял действие 🤔", reply_markup=MAIN_KB)

# =========================================================
# APPLICATION
# =========================================================
def build_telegram_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app

# =========================================================
# FASTAPI WEBHOOK
# =========================================================
def mount(fastapi_app: FastAPI) -> None:
    tg_app = build_telegram_application()
    fastapi_app.state.telegram_app = tg_app

    async def _startup() -> None:
        await tg_app.initialize()
        await tg_app.start()

        if PUBLIC_URL:
            await tg_app.bot.set_webhook(
                url=f"{PUBLIC_URL}{WEBHOOK_PATH}",
                secret_token=WEBHOOK_SECRET or None,
                drop_pending_updates=True,
            )

    async def _shutdown() -> None:
        await tg_app.stop()
        await tg_app.shutdown()

    fastapi_app.add_event_handler("startup", _startup)
    fastapi_app.add_event_handler("shutdown", _shutdown)

    @fastapi_app.post(WEBHOOK_PATH)
    async def telegram_webhook(request: Request) -> dict[str, Any]:
        if WEBHOOK_SECRET:
            if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
                raise HTTPException(status_code=403, detail="Bad webhook secret")

        payload = await request.json()
        update = Update.de_json(payload, tg_app.bot)
        await tg_app.process_update(update)
        return {"ok": True}
