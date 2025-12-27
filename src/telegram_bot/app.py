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
# Главное меню
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

# --- helpers
def _norm_menu(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[^\w\sа-яё-]", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# -----------------------------
# SAFETY: no Markdown/HTML, safe send/edit
# -----------------------------
def _tg_safe_text(s: str) -> str:
    # максимально безопасно для Telegram: без markdown/html, без нулевых байт
    s = (s or "").replace("\u0000", "")
    return s.strip()


def _split_chunks(s: str, chunk: int = 3900) -> list[str]:
    s = s or ""
    if len(s) <= chunk:
        return [s]
    return [s[i : i + chunk] for i in range(0, len(s), chunk)]


async def _safe_reply(message, text: str, reply_markup=None) -> None:
    text = _tg_safe_text(text)
    for ch in _split_chunks(text):
        await message.reply_text(ch, reply_markup=reply_markup)


async def _safe_edit_or_reply(message, text: str, reply_markup=None) -> None:
    """
    Пытаемся edit_text (если это сообщение от бота).
    Если Telegram ругнётся (entities/длина/что угодно) — отправим новым сообщением.
    """
    text = _tg_safe_text(text)
    # Telegram лимит 4096, но лучше не упираться
    chunks = _split_chunks(text)
    # edit умеет только 1 текст, поэтому:
    first = chunks[0] if chunks else ""
    try:
        await message.edit_text(first, reply_markup=reply_markup)
    except BadRequest as e:
        logger.warning("edit_text failed (%s) -> fallback to reply_text", e)
        await message.reply_text(first, reply_markup=reply_markup)

    # остаток докидываем reply_text
    for ch in chunks[1:]:
        await message.reply_text(ch, reply_markup=reply_markup)


# -----------------------------
# Парсинг матчей из текста backend
# Ожидаем:
# • СКА — ЦСКА (КХЛ) — id: demo_hockey_001
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


# -----------------------------
# Keyboards
# -----------------------------
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
    await _safe_reply(
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
    if norm in {"матчи сегодня"}:
        await _safe_reply(update.message, "🏟 Выбери спорт:", reply_markup=kb_sports())
        await _safe_reply(update.message, "Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # --- Профиль ---
    if norm in {"профиль", "мой профиль", "статы", "статистика"}:
        reply = await call_agent_local(user_id, "профиль")
        await _safe_reply(update.message, reply, reply_markup=MAIN_KB)
        return

    # --- Стратегия эксперта ---
    if norm in {"стратегия эксперта", "стратегия", "эксперт", "эксперт сегодня"}:
        reply = await call_agent_local(user_id, "стратегия")
        await _safe_reply(update.message, reply, reply_markup=MAIN_KB)
        return

    # --- AI аналитика (как пользоваться) ---
    if norm in {"ai аналитика", "аналитика", "ии аналитика"}:
        await _safe_reply(
            update.message,
            (
                "Как пользоваться:\n"
                "1) 🏟 Матчи сегодня\n"
                "2) спорт → матч\n"
                "3) в матче нажми: 📊 Обзор или 🧠 1X2/Тотал/Фора\n"
                "4) LIVE: 🟢 LIVE или 🔄 Обновить\n\n"
                "Диагностика: llm ping, env, version, last_error"
            ),
            reply_markup=MAIN_KB,
        )
        return

    # --- Остальное: в агента ---
    reply = await call_agent_local(user_id, text)
    await _safe_reply(update.message, reply, reply_markup=MAIN_KB)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return

    data = query.data or ""
    user_id = query.from_user.id

    # обязательно отвечаем на callback (чтобы не крутилось)
    try:
        await query.answer()
    except Exception:
        pass

    logger.info("tg.callback user_id=%s data=%r", user_id, data)
    await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.TYPING)

    # BACK -> SPORTS
    if data == "BACK:SPORTS":
        await _safe_edit_or_reply(query.message, "🏟 Выбери спорт:", reply_markup=kb_sports())
        await _safe_reply(query.message, "Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # BACK -> MATCHES (по последнему выбранному спорту)
    if data == "BACK:MATCHES":
        match_buttons = context.user_data.get("last_match_buttons") or []
        if match_buttons:
            await _safe_edit_or_reply(query.message, "Выбери матч:", reply_markup=kb_matches(match_buttons))
        else:
            await _safe_edit_or_reply(query.message, "🏟 Выбери спорт:", reply_markup=kb_sports())
        await _safe_reply(query.message, "Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # SPORT -> список матчей
    if data.startswith("SPORT:"):
        sport_key = data.split(":", 1)[1]
        reply = await call_agent_local(user_id, f"матчи сегодня {sport_key}")

        match_buttons = extract_match_buttons(reply)
        context.user_data["last_match_buttons"] = match_buttons

        # редактируем текущее сообщение (если возможно), иначе просто reply
        await _safe_edit_or_reply(query.message, reply)

        if not match_buttons:
            await _safe_reply(query.message, "🏟 Выбери спорт:", reply_markup=kb_sports())
            await _safe_reply(query.message, "Меню ниже 👇", reply_markup=MAIN_KB)
            return

        await _safe_reply(query.message, "Выбери матч:", reply_markup=kb_matches(match_buttons))
        await _safe_reply(query.message, "Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # MATCH -> экран матча + хаб кнопок
    if data.startswith("MATCH:"):
        match_id = data.split(":", 1)[1]
        reply = await call_agent_local(user_id, f"матч {match_id}")

        await _safe_edit_or_reply(query.message, reply)
        await _safe_reply(query.message, "Ещё действия:", reply_markup=kb_match_hub(match_id))
        await _safe_reply(query.message, "Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # UI actions -> ui match <id> <mode> <action>
    if data.startswith("UI:"):
        parts = data.split(":")
        if len(parts) != 4:
            await _safe_reply(query.message, "Некорректная команда UI.", reply_markup=MAIN_KB)
            return

        _, match_id, mode, action = parts
        reply = await call_agent_local(user_id, f"ui match {match_id} {mode} {action}")

        # самый частый источник 400 BadRequest был тут из-за Markdown — теперь просто plain text
        await _safe_edit_or_reply(query.message, reply, reply_markup=kb_match_hub(match_id))
        await _safe_reply(query.message, "Меню ниже 👇", reply_markup=MAIN_KB)
        return

    await _safe_reply(query.message, "Не понял действие 🤔", reply_markup=MAIN_KB)


# -----------------------------
# Error handler (чтобы не “молчало”)
# -----------------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Telegram handler error: %s", context.error)


# -----------------------------
# Application factory
# -----------------------------
def build_telegram_application() -> Application:
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_error_handler(on_error)

    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CallbackQueryHandler(handle_callback))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
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
