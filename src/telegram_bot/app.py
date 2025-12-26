# src/telegram_bot/app.py
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
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").strip().rstrip("/")
WEBHOOK_PATH = (os.getenv("TELEGRAM_WEBHOOK_PATH") or "/telegram/webhook").strip()
WEBHOOK_SECRET = (os.getenv("TELEGRAM_WEBHOOK_SECRET") or "").strip()

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

MAIN_KB = ReplyKeyboardMarkup(
    [
        ["🏟 Матчи сегодня"],
        ["🧠 AI Аналитика", "👤 Стратегия эксперта"],
        ["📊 Профиль"],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)

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
        buf.append(InlineKeyboardButton(label, callback_data=f"s|{key}"))
        if len(buf) == 2:
            rows.append(buf)
            buf = []
    if buf:
        rows.append(buf)
    return InlineKeyboardMarkup(rows)


def kb_matches(match_buttons: list[tuple[str, str]], sport_key: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(title, callback_data=f"m|{match_id}|pre|open")]
        for match_id, title in match_buttons
    ]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back|sports")])
    return InlineKeyboardMarkup(rows)


def kb_match_main(match_id: str, mode: str, sport_key: str) -> InlineKeyboardMarkup:
    mode = (mode or "pre").lower()
    if mode == "live":
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🟢 LIVE-контекст", callback_data=f"ui|{match_id}|live|overview")],
                [InlineKeyboardButton("🔄 Обновить LIVE", callback_data=f"ui|{match_id}|live|refresh")],
                [
                    InlineKeyboardButton("📊 PRE: обзор", callback_data=f"ui|{match_id}|pre|overview"),
                    InlineKeyboardButton("⬅️ Назад", callback_data=f"back|matches|{sport_key}"),
                ],
            ]
        )

    # prematch
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Обзор рынков", callback_data=f"ui|{match_id}|pre|overview")],
            [
                InlineKeyboardButton("🧠 1X2", callback_data=f"ui|{match_id}|pre|moneyline"),
                InlineKeyboardButton("🧠 Total", callback_data=f"ui|{match_id}|pre|total"),
            ],
            [InlineKeyboardButton("🧠 Фора", callback_data=f"ui|{match_id}|pre|handicap")],
            [
                InlineKeyboardButton("🟢 Перейти в LIVE (MVP)", callback_data=f"ui|{match_id}|live|overview"),
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"back|matches|{sport_key}")],
        ]
    )


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


async def call_agent_local(user_id: int, message: str) -> str:
    from ..parsing import run_dialog_agent
    return await run_dialog_agent(user_id=user_id, message=message)


def _detect_mode_from_text(text: str) -> str:
    # простая эвристика: если где-то явно LIVE
    s = (text or "").lower()
    return "live" if "live" in s or "лайв" in s else "pre"


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

    if norm == "матчи сегодня":
        await update.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
        await update.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    if norm in {"стратегия эксперта", "стратегия", "эксперт", "эксперт сегодня"}:
        reply = await call_agent_local(user_id, "стратегия")
        await update.message.reply_text(reply, reply_markup=MAIN_KB, parse_mode="Markdown")
        return

    if norm in {"ai аналитика", "аналитика", "ии аналитика"}:
        await update.message.reply_text(
            "Как пользоваться:\n"
            "1) *🏟 Матчи сегодня* → спорт → матч\n"
            "2) Нажми *📊 Обзор рынков* или кнопку рынка\n"
            "3) Для LIVE используй кнопку *🟢 Перейти в LIVE*\n\n"
            "Диагностика:\n"
            "• `version`\n"
            "• `env`\n"
            "• `llm ping`",
            reply_markup=MAIN_KB,
            parse_mode="Markdown",
        )
        return

    if "профиль" in norm:
        reply = await call_agent_local(user_id, "профиль")
        await update.message.reply_text(reply, reply_markup=MAIN_KB, parse_mode="Markdown")
        return

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
    if data == "back|sports":
        await query.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    if data.startswith("back|matches|"):
        sport_key = data.split("|", 2)[2]
        reply = await call_agent_local(user_id, f"матчи сегодня {sport_key}")
        match_buttons = extract_match_buttons(reply)
        await query.message.reply_text(reply, parse_mode="Markdown")
        await query.message.reply_text("Выбери матч:", reply_markup=kb_matches(match_buttons, sport_key))
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # sport select
    if data.startswith("s|"):
        sport_key = data.split("|", 1)[1]
        reply = await call_agent_local(user_id, f"матчи сегодня {sport_key}")
        match_buttons = extract_match_buttons(reply)

        await query.message.reply_text(reply, parse_mode="Markdown")
        if not match_buttons:
            await query.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
            await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
            return

        await query.message.reply_text("Выбери матч:", reply_markup=kb_matches(match_buttons, sport_key))
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # match open
    if data.startswith("m|"):
        # m|<match_id>|<mode>|open
        parts = data.split("|")
        match_id = parts[1]
        sport_key = "hockey"  # fallback
        # попробуем восстановить sport из match_id (demo_*_xxx)
        if match_id.startswith("demo_"):
            seg = match_id.split("_", 2)
            if len(seg) >= 2:
                sport_key = seg[1]

        # даём короткий заголовок + основную клавиатуру
        reply = await call_agent_local(user_id, f"матч {match_id}")
        mode = _detect_mode_from_text(reply)

        await query.message.reply_text(reply, parse_mode="Markdown")
        await query.message.reply_text(
            "Выбери действие:",
            reply_markup=kb_match_main(match_id, mode, sport_key),
            parse_mode="Markdown",
        )
        await query.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    # UI actions
    if data.startswith("ui|"):
        # ui|<match_id>|<mode>|<action>
        _, match_id, mode, action = data.split("|", 3)

        # для action=moneyline/total/handicap в UI используем action как “action”
        reply = await call_agent_local(user_id, f"ui match {match_id} {mode} {action}")

        sport_key = "hockey"
        if match_id.startswith("demo_"):
            seg = match_id.split("_", 2)
            if len(seg) >= 2:
                sport_key = seg[1]

        await query.message.reply_text(reply, parse_mode="Markdown")
        await query.message.reply_text(
            "Ещё действия:",
            reply_markup=kb_match_main(match_id, mode, sport_key),
            parse_mode="Markdown",
        )
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
