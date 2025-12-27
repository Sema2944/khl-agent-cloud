# src/telegram_bot/app.py
from __future__ import annotations

import logging
import os
import re
from typing import Any

from fastapi import FastAPI, Request, HTTPException
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

# -----------------------------
# ENV
# -----------------------------
BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").strip().rstrip("/")
WEBHOOK_PATH = (os.getenv("TELEGRAM_WEBHOOK_PATH") or "/telegram/webhook").strip()
WEBHOOK_SECRET = (os.getenv("TELEGRAM_WEBHOOK_SECRET") or "").strip()  # optional

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

# -----------------------------
# Главное меню
# -----------------------------
MAIN_KB = ReplyKeyboardMarkup(
    [
        ["🏟 Матчи сегодня"],
        ["🧠 AI Аналитика", "👤 Стратегия эксперта на сегодня"],
        ["📊 Профиль"],
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
    rows: list[list[InlineKeyboardButton]] = []
    buf: list[InlineKeyboardButton] = []
    for key, label in SPORTS:
        buf.append(InlineKeyboardButton(label, callback_data=f"SPORT:{key}"))
        if len(buf) == 2:
            rows.append(buf)
            buf = []
    if buf:
        rows.append(buf)
    return InlineKeyboardMarkup(rows)


def kb_matches(match_buttons: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(title, callback_data=f"MATCH:{match_id}")]
        for match_id, title in match_buttons
    ]
    rows.append([InlineKeyboardButton("⬅️ Назад к спорту", callback_data="BACK:SPORTS")])
    return InlineKeyboardMarkup(rows)


def kb_match_hub(match_id: str) -> InlineKeyboardMarkup:
    """
    Ключевая клавиатура внутри матча (PRE + LIVE).
    """
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Обзор рынков (PREMATCH)", callback_data=f"UI:{match_id}:pre:overview")],
            [
                InlineKeyboardButton("🧠 Подробнее про 1X2", callback_data=f"UI:{match_id}:pre:moneyline"),
                InlineKeyboardButton("🧠 Подробнее про Total", callback_data=f"UI:{match_id}:pre:total"),
            ],
            [InlineKeyboardButton("🧠 Подробнее про Фору", callback_data=f"UI:{match_id}:pre:handicap")],
            [
                InlineKeyboardButton("🟢 LIVE-обзор", callback_data=f"UI:{match_id}:live:overview"),
                InlineKeyboardButton("🔄 Обновить LIVE", callback_data=f"UI:{match_id}:live:refresh"),
            ],
            [InlineKeyboardButton("⬅️ Назад к списку матчей", callback_data="BACK:SPORTS")],
        ]
    )


# -----------------------------
# Парсинг match_id из текста backend
# Ожидаем:
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
        title = re.sub(r"\s*—\s*id:\s*`?.+`?\s*$", "", line).strip()
        title = title.lstrip("•").strip()
        if match_id and title:
            buttons.append((match_id, title))
    return buttons


def _norm_menu(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[^\w\sа-яё-]", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# -----------------------------
# Локальный вызов агента (без HTTP)
# -----------------------------
async def call_agent_local(user_id: int, message: str) -> str:
    from ..parsing import run_dialog_agent
    return await run_dialog_agent(user_id=user_id, message=message)


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

    logger.info("tg.handle_message user_id=%s text=%r", user_id, text)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # Главное меню: Матчи сегодня
    if norm in {"матчи сегодня"}:
        await update.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
        await update.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # Главное меню: Стратегия эксперта
    if norm in {"стратегия эксперта на сегодня", "стратегия эксперта", "стратегия", "эксперт", "эксперт сегодня"}:
        reply = await call_agent_local(user_id, "стратегия")
        await update.message.reply_text(reply, reply_markup=MAIN_KB, parse_mode="Markdown")
        return

    # Главное меню: Профиль
    if "профиль" in norm:
        reply = await call_agent_local(user_id, "профиль")
        await update.message.reply_text(reply, reply_markup=MAIN_KB, parse_mode="Markdown")
        return

    # Главное меню: AI аналитика
    if norm in {"ai аналитика", "аналитика", "ии аналитика"}:
        await update.message.reply_text(
            "Как пользоваться:\n"
            "1) нажми *🏟 Матчи сегодня*\n"
            "2) выбери спорт → матч\n"
            "3) внутри матча нажми *📊 Обзор рынков* или *Подробнее*\n"
            "4) для LIVE нажми *🟢 LIVE-обзор*\n\n"
            "Диагностика:\n"
            "• `llm ping`\n"
            "• `env`\n"
            "• `version`",
            reply_markup=MAIN_KB,
            parse_mode="Markdown",
        )
        return

    # Остальное: проброс в агента
    reply = await call_agent_local(user_id, text)
    await update.message.reply_text(reply, reply_markup=MAIN_KB, parse_mode="Markdown")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    user_id = query.from_user.id
    await query.answer()

    logger.info("tg.callback user_id=%s data=%r", user_id, data)

    # BACK
    if data == "BACK:SPORTS":
        await query.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # SPORT -> список матчей
    if data.startswith("SPORT:"):
        sport_key = data.split(":", 1)[1]
        reply = await call_agent_local(user_id, f"матчи сегодня {sport_key}")

        match_buttons = extract_match_buttons(reply)
        await query.message.reply_text(reply, parse_mode="Markdown")

        if not match_buttons:
            await query.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
            await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
            return

        await query.message.reply_text("Выбери матч:", reply_markup=kb_matches(match_buttons))
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # MATCH -> экран матча + HUB кнопки
    if data.startswith("MATCH:"):
        match_id = data.split(":", 1)[1]
        reply = await call_agent_local(user_id, f"матч {match_id}")
        await query.message.reply_text(reply, parse_mode="Markdown")
        await query.message.reply_text("Действия по матчу:", reply_markup=kb_match_hub(match_id))
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # UI actions (PRE/LIVE)
    # UI:<match_id>:<mode>:<action>
    if data.startswith("UI:"):
        _, match_id, mode, action = data.split(":", 3)
        # команда в parsing.py:
        # ui match <match_id> <mode> <action>
        reply = await call_agent_local(user_id, f"ui match {match_id} {mode} {action}")
        await query.message.reply_text(reply, parse_mode="Markdown")
        await query.message.reply_text("Ещё действия:", reply_markup=kb_match_hub(match_id))
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    await query.message.reply_text("Не понял действие 🤔", reply_markup=MAIN_KB)


def build_telegram_application() -> Application:
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CallbackQueryHandler(handle_callback))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return tg_app


def mount(fastapi_app: FastAPI) -> None:
    """
    Регистрирует /telegram/webhook и lifecycle-хуки.
    НИКАКОГО polling.
    """
    tg_app = build_telegram_application()
    fastapi_app.state.telegram_app = tg_app

    async def _startup() -> None:
        await tg_app.initialize()
        await tg_app.start()

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

    async def _shutdown() -> None:
        try:
            await tg_app.stop()
        finally:
            await tg_app.shutdown()

    fastapi_app.add_event_handler("startup", _startup)
    fastapi_app.add_event_handler("shutdown", _shutdown)

    @fastapi_app.post(WEBHOOK_PATH)
    async def telegram_webhook(request: Request) -> dict[str, Any]:
        if WEBHOOK_SECRET:
            got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if got != WEBHOOK_SECRET:
                raise HTTPException(status_code=403, detail="Bad webhook secret")

        payload = await request.json()
        update = Update.de_json(payload, tg_app.bot)
        await tg_app.process_update(update)
        return {"ok": True}
