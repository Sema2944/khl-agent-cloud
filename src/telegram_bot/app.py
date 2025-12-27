# src/telegram_bot/app.py  (v6)
from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

from fastapi import FastAPI, Request, HTTPException
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest
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
# Главное меню (ReplyKeyboard)
# -----------------------------
MAIN_KB = ReplyKeyboardMarkup(
    [
        ["🏟 Матчи сегодня"],
        ["🧠 AI Аналитика", "👤 Стратегия эксперта"],
        ["📊 Профиль"],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)

# -----------------------------
# Inline клавиатуры
# -----------------------------
SPORTS = [
    ("hockey", "🏒 Хоккей"),
    ("football", "⚽ Футбол"),
    ("basketball", "🏀 Баскетбол"),
    ("tennis", "🎾 Теннис"),
    ("esports", "🎮 Киберспорт"),
]

# ожидаем строки типа:
# • СКА — ЦСКА (КХЛ) — id: demo_hockey_001
ID_RE = re.compile(r"id:\s*`?([a-zA-Z0-9_\-:.]{4,120})`?", re.IGNORECASE)


# -----------------------------
# Helpers
# -----------------------------
def _norm_menu(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[^\w\sа-яё-]", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


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
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="BACK:SPORTS")])
    return InlineKeyboardMarkup(rows)


def kb_match_hub(match_id: str) -> InlineKeyboardMarkup:
    """
    Компактные кнопки, читаемые на iOS/Android.
    Все AI-разборы идут через команду: ui match <id> <mode> <action>
    """
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


async def call_agent_local(user_id: int, message: str) -> str:
    from ..parsing import run_dialog_agent
    return await run_dialog_agent(user_id=user_id, message=message)


async def _safe_send(
    update_or_query,
    text: str,
    *,
    reply_markup=None,
    as_edit: bool = False,
) -> None:
    """
    Отправка/редактирование без Markdown (чтобы не ловить BadRequest по entities).
    Фоллбек: если edit не удался — отправим новым сообщением.
    """
    text = (text or "").strip()
    if not text:
        text = "…"

    try:
        if as_edit and hasattr(update_or_query, "message") and update_or_query.message:
            await update_or_query.message.edit_text(text, reply_markup=reply_markup)
        elif hasattr(update_or_query, "message") and update_or_query.message:
            await update_or_query.message.reply_text(text, reply_markup=reply_markup)
        else:
            # update.message case
            await update_or_query.reply_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        logger.warning("Telegram BadRequest (entities/UI). Fallback to plain message. err=%s", e)
        try:
            # если это был edit — шлём новое сообщение
            if hasattr(update_or_query, "message") and update_or_query.message:
                await update_or_query.message.reply_text(text, reply_markup=reply_markup)
        except Exception:
            logger.exception("Failed to fallback send after BadRequest")


# -----------------------------
# Handlers
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await _safe_send(
        update.message,
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

    # --- Главное меню: Матчи сегодня ---
    if norm == "матчи сегодня":
        await _safe_send(update.message, "🏟 Выбери спорт:", reply_markup=kb_sports())
        return

    # --- Профиль ---
    if norm in {"профиль", "мой профиль", "статы", "статистика"}:
        reply = await call_agent_local(user_id, "профиль")
        await _safe_send(update.message, reply, reply_markup=MAIN_KB)
        return

    # --- Стратегия эксперта ---
    if norm in {"стратегия эксперта", "стратегия", "эксперт", "эксперт сегодня"}:
        reply = await call_agent_local(user_id, "стратегия")
        await _safe_send(update.message, reply, reply_markup=MAIN_KB)
        return

    # --- AI аналитика (help) ---
    if norm in {"ai аналитика", "аналитика", "ии аналитика"}:
        await _safe_send(
            update.message,
            "Как пользоваться:\n"
            "1) 🏟 Матчи сегодня\n"
            "2) спорт → матч\n"
            "3) в матче нажми: 📊 Обзор или 🧠 1X2/Тотал/Фора\n"
            "4) LIVE: 🟢 LIVE или 🔄 Обновить\n\n"
            "Диагностика: llm ping, env, version, last_error",
            reply_markup=MAIN_KB,
        )
        return

    # --- Остальное: в агента ---
    reply = await call_agent_local(user_id, text)
    await _safe_send(update.message, reply, reply_markup=MAIN_KB)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return

    data = query.data or ""
    user_id = query.from_user.id
    await query.answer()

    logger.info("tg.callback user_id=%s data=%r", user_id, data)
    await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.TYPING)

    # BACK -> SPORTS
    if data == "BACK:SPORTS":
        await _safe_send(query, "🏟 Выбери спорт:", reply_markup=kb_sports(), as_edit=True)
        return

    # BACK -> MATCHES
    if data == "BACK:MATCHES":
        match_buttons = context.user_data.get("last_match_buttons") or []
        if match_buttons:
            await _safe_send(query, "Выбери матч:", reply_markup=kb_matches(match_buttons), as_edit=True)
        else:
            await _safe_send(query, "🏟 Выбери спорт:", reply_markup=kb_sports(), as_edit=True)
        return

    # SPORT -> список матчей
    if data.startswith("SPORT:"):
        sport_key = data.split(":", 1)[1]
        reply = await call_agent_local(user_id, f"матчи сегодня {sport_key}")

        match_buttons = extract_match_buttons(reply)
        context.user_data["last_match_buttons"] = match_buttons

        # редактируем текущее сообщение (чтобы не спамить “меню ниже”)
        if match_buttons:
            await _safe_send(query, reply, as_edit=True)
            await _safe_send(query.message, "Выбери матч:", reply_markup=kb_matches(match_buttons))
        else:
            await _safe_send(query, reply, as_edit=True)
            await _safe_send(query.message, "🏟 Выбери спорт:", reply_markup=kb_sports())
        return

    # MATCH -> экран матча + хаб кнопок
    if data.startswith("MATCH:"):
        match_id = data.split(":", 1)[1]
        reply = await call_agent_local(user_id, f"матч {match_id}")

        # 1) редактируем текущую карточку списка матчей на карточку матча
        await _safe_send(query, reply, as_edit=True)

        # 2) отдельно отправляем кнопки действий
        await _safe_send(query.message, "Выбери действие:", reply_markup=kb_match_hub(match_id))
        return

    # UI actions -> ui match <id> <mode> <action>
    if data.startswith("UI:"):
        parts = data.split(":")
        if len(parts) != 4:
            await _safe_send(query.message, "Некорректная команда UI.", reply_markup=MAIN_KB)
            return

        _, match_id, mode, action = parts

        # ВАЖНО: иногда OpenAI/рендер фейлит — мы всё равно покажем текст (без Markdown)
        reply = await call_agent_local(user_id, f"ui match {match_id} {mode} {action}")

        # редактируем последнее “Выбери действие” или матч-карточку — не важно: Telegram решит по message
        await _safe_send(query, reply, reply_markup=kb_match_hub(match_id), as_edit=True)
        return

    await _safe_send(query.message, "Не понял действие 🤔", reply_markup=MAIN_KB)


# -----------------------------
# Errors handler (чтобы не было "No error handlers")
# -----------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled telegram error: %s", context.error)


# -----------------------------
# Application factory
# -----------------------------
def build_telegram_application() -> Application:
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CallbackQueryHandler(handle_callback))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    tg_app.add_error_handler(error_handler)
    return tg_app


# -----------------------------
# FastAPI mount (webhook-only)
# -----------------------------
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
