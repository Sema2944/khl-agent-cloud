# src/telegram_bot/app.py  (v7.0 - UX polish: match/overview + clean LIVE paywall)
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
    Message,
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

from ..user_store import get_or_create_user
from ..entitlements import get_effective_entitlements

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
        ["📊 Профиль", "⭐ Premium"],
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
    """
    Парсим список матчей из ответа агента.
    Возвращаем список (match_id, title).
    """
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


def _disclaimer() -> str:
    return "ℹ️ Аналитический материал. Не является рекомендацией."


def _wrap_screen(title: str, body: str, *, footer: bool = True) -> str:
    body = (body or "").strip()
    title = (title or "").strip()
    parts = []
    if title:
        parts.append(f"{title}\n")
    if body:
        parts.append(body)
    if footer:
        parts.append(f"\n\n{_disclaimer()}")
    return "\n".join(parts).strip() or "…"


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
    rows.append([InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")])
    return InlineKeyboardMarkup(rows)


def kb_matches(match_buttons: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(title, callback_data=f"MATCH:{match_id}")]
        for match_id, title in match_buttons
    ]
    rows.append([InlineKeyboardButton("⬅️ Назад к видам спорта", callback_data="BACK:SPORTS")])
    rows.append([InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")])
    return InlineKeyboardMarkup(rows)


def kb_match_hub(match_id: str) -> InlineKeyboardMarkup:
    # “Хаб матча”: обзор рынков + блок прематч + блок LIVE
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Обзор рынков", callback_data=f"UI:{match_id}:pre:overview")],
            [
                InlineKeyboardButton("🧠 1X2", callback_data=f"UI:{match_id}:pre:moneyline"),
                InlineKeyboardButton("🧠 Тотал", callback_data=f"UI:{match_id}:pre:total"),
            ],
            [InlineKeyboardButton("🧠 Фора", callback_data=f"UI:{match_id}:pre:handicap")],
            [
                InlineKeyboardButton("🟢 LIVE-обзор", callback_data=f"UI:{match_id}:live:overview"),
                InlineKeyboardButton("🔄 LIVE обновить", callback_data=f"UI:{match_id}:live:refresh"),
            ],
            [InlineKeyboardButton("⬅️ К матчам", callback_data="BACK:MATCHES")],
            [InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")],
        ]
    )


def kb_premium() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⭐ Оформить Premium", callback_data="PAY:PREMIUM")],
            [InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")],
        ]
    )


def kb_live_paywall(match_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⭐ Оформить Premium", callback_data="PAY:PREMIUM")],
            [InlineKeyboardButton("⬅️ К матчу", callback_data=f"MATCH:{match_id}")],
            [InlineKeyboardButton("🏠 В меню", callback_data="BACK:MENU")],
        ]
    )


def _premium_text(tg_user_id: int) -> str:
    ent = get_effective_entitlements(int(tg_user_id))
    tier = getattr(ent, "tier", "free").upper()

    ai_daily_limit = getattr(ent, "ai_daily_limit", 0)
    daily_ai_left = getattr(ent, "daily_ai_left", 0)

    live_refresh_daily_limit = getattr(ent, "live_refresh_daily_limit", 0)
    live_refresh_left = getattr(ent, "live_refresh_left", 0)

    live_min_interval_sec = getattr(ent, "live_min_interval_sec", 0)

    return (
        "⭐ Premium\n\n"
        f"Текущий доступ: {tier}\n"
        f"AI/день: {daily_ai_left}/{ai_daily_limit}\n"
        f"LIVE refresh/день: {live_refresh_left}/{live_refresh_daily_limit}\n"
        f"Пауза между LIVE: {int(live_min_interval_sec)} сек\n\n"
        "Что даст Premium:\n"
        "• LIVE без ограничений\n"
        "• больше AI-лимитов\n"
        "• расширенные фичи (добавим)\n"
    ).strip()


async def call_agent_local(user_id: int, message: str) -> str:
    from ..parsing import run_dialog_agent
    return await run_dialog_agent(user_id=user_id, message=message)


async def _edit_or_send(
    msg: Message,
    text: str,
    *,
    reply_markup=None,
    force_new: bool = False,
) -> None:
    text = (text or "").strip() or "…"

    if not force_new:
        try:
            await msg.edit_text(text, reply_markup=reply_markup)
            return
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            logger.warning("edit_text BadRequest -> fallback to send. err=%s", e)
        except Exception:
            logger.exception("edit_text failed -> fallback to send")

    try:
        await msg.reply_text(text, reply_markup=reply_markup)
    except Exception:
        logger.exception("reply_text failed")


async def _typing_safe(context: ContextTypes.DEFAULT_TYPE, chat_id: Optional[int]) -> None:
    if not chat_id:
        return
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        return


def _save_match_map(context: ContextTypes.DEFAULT_TYPE, match_buttons: list[tuple[str, str]]) -> None:
    """
    Сохраняем map match_id->title в user_data, чтобы красиво подписывать экран матча.
    """
    mp = {mid: title for mid, title in (match_buttons or [])}
    context.user_data["match_title_map"] = mp


def _get_match_title(context: ContextTypes.DEFAULT_TYPE, match_id: str) -> Optional[str]:
    mp = context.user_data.get("match_title_map") or {}
    return mp.get(match_id)


def _paywall_live_text() -> str:
    return (
        "🔒 LIVE доступен в Premium\n\n"
        "Что получишь:\n"
        "• LIVE-обзор и обновления\n"
        "• меньше ограничений\n\n"
        "Хочешь — подключим Premium."
    ).strip()


# -----------------------------
# Handlers
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    tg_user = update.effective_user
    # ВАЖНО: user_store.get_or_create_user принимает только tg_user_id позиционно, остальное — keyword-only
    get_or_create_user(
        tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
    )

    await update.message.reply_text(
        "✅ Я на связи.\n\nВыбирай действие кнопками ниже 👇",
        reply_markup=MAIN_KB,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        _wrap_screen(
            "🧠 AI Аналитика",
            "Как пользоваться:\n"
            "1) 🏟 Матчи сегодня\n"
            "2) спорт → матч\n"
            "3) в матче нажми: 📊 Обзор рынков или 🧠 1X2/Тотал/Фора\n"
            "4) LIVE: 🟢 LIVE-обзор или 🔄 LIVE обновить\n\n"
            "Диагностика: llm ping, env, version, last_error",
            footer=False,
        ),
        reply_markup=MAIN_KB,
    )


async def premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    tg_user = update.effective_user
    get_or_create_user(
        tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
    )

    await update.message.reply_text(_premium_text(tg_user.id), reply_markup=kb_premium())


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    tg_user = update.effective_user
    user_id = tg_user.id
    text = update.message.text or ""
    norm = _norm_menu(text)

    get_or_create_user(
        user_id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
    )

    logger.info("tg.handle_message user_id=%s text=%r", user_id, text)
    await _typing_safe(context, update.effective_chat.id if update.effective_chat else None)

    # Жёсткая маршрутизация меню (чтобы не было “странных” ответов от агента)
    if norm == "матчи сегодня":
        await update.message.reply_text("🏟 Выбери спорт:", reply_markup=kb_sports())
        return

    if norm in {"профиль", "мой профиль", "статы", "статистика"}:
        reply = await call_agent_local(user_id, "профиль")
        await update.message.reply_text(reply, reply_markup=MAIN_KB)
        return

    if norm in {"стратегия эксперта", "стратегия", "эксперт", "эксперт сегодня"}:
        reply = await call_agent_local(user_id, "стратегия")
        await update.message.reply_text(reply, reply_markup=MAIN_KB)
        return

    if norm in {"ai аналитика", "аналитика", "ии аналитика"}:
        await help_cmd(update, context)
        return

    if norm in {"premium", "премиум"}:
        await premium_cmd(update, context)
        return

    # Любой другой текст — в агента
    reply = await call_agent_local(user_id, text)
    await update.message.reply_text(reply, reply_markup=MAIN_KB)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return

    data = query.data or ""
    tg_user_id = query.from_user.id
    await query.answer()

    logger.info("tg.callback user_id=%s data=%r", tg_user_id, data)
    await _typing_safe(context, query.message.chat_id)

    tg_user = query.from_user
    get_or_create_user(
        tg_user_id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
    )

    screen_msg: Message = query.message

    if data == "BACK:MENU":
        # ReplyKeyboard не редактируется, поэтому шлём новое сообщение
        await _edit_or_send(
            screen_msg,
            "Выбирай действие кнопками ниже 👇",
            reply_markup=MAIN_KB,
            force_new=True,
        )
        return

    if data == "BACK:SPORTS":
        await _edit_or_send(screen_msg, "🏟 Выбери спорт:", reply_markup=kb_sports())
        return

    if data == "BACK:MATCHES":
        last_text = context.user_data.get("last_matches_text")
        last_buttons = context.user_data.get("last_match_buttons") or []
        last_sport_key = context.user_data.get("last_sport_key")

        if not last_text and last_sport_key:
            last_text = await call_agent_local(tg_user_id, f"матчи сегодня {last_sport_key}")
            last_buttons = extract_match_buttons(last_text)
            context.user_data["last_matches_text"] = last_text
            context.user_data["last_match_buttons"] = last_buttons
            _save_match_map(context, last_buttons)

        if last_text and last_buttons:
            await _edit_or_send(screen_msg, last_text, reply_markup=kb_matches(last_buttons))
        else:
            await _edit_or_send(screen_msg, "🏟 Выбери спорт:", reply_markup=kb_sports())
        return

    if data.startswith("SPORT:"):
        sport_key = data.split(":", 1)[1].strip()
        context.user_data["last_sport_key"] = sport_key

        # Здесь только список матчей. Никаких LIVE/обзоров.
        reply = await call_agent_local(tg_user_id, f"матчи сегодня {sport_key}")
        match_buttons = extract_match_buttons(reply)

        context.user_data["last_matches_text"] = reply
        context.user_data["last_match_buttons"] = match_buttons
        _save_match_map(context, match_buttons)

        if match_buttons:
            await _edit_or_send(screen_msg, reply, reply_markup=kb_matches(match_buttons))
        else:
            await _edit_or_send(screen_msg, reply, reply_markup=kb_sports())
        return

    if data.startswith("MATCH:"):
        match_id = data.split(":", 1)[1].strip()
        context.user_data["active_match_id"] = match_id

        # Берём title матча (если есть) и делаем аккуратную шапку
        title = _get_match_title(context, match_id)
        raw = await call_agent_local(tg_user_id, f"матч {match_id}")

        header = "🏟 Матч"
        if title:
            header = f"🏟 {title}"

        text_out = _wrap_screen(header, raw, footer=True)
        await _edit_or_send(screen_msg, text_out, reply_markup=kb_match_hub(match_id))
        return

    if data.startswith("UI:"):
        parts = data.split(":")
        if len(parts) != 4:
            await _edit_or_send(screen_msg, "Некорректная команда.", reply_markup=MAIN_KB)
            return

        _, match_id, mode, action = parts
        context.user_data["active_match_id"] = match_id

        # -------- PAYWALL: только для LIVE --------
        if mode == "live":
            ent = get_effective_entitlements(int(tg_user_id))
            can_live = bool(getattr(ent, "can_live", False))
            can_live_refresh = bool(getattr(ent, "can_live_refresh", False))

            if not can_live:
                await _edit_or_send(
                    screen_msg,
                    _paywall_live_text(),
                    reply_markup=kb_live_paywall(match_id),
                )
                return

            if action == "refresh" and not can_live_refresh:
                await _edit_or_send(
                    screen_msg,
                    "🔒 Лимит LIVE-обновлений исчерпан (Free).\n\nОформи Premium — и обновляй без ограничений.",
                    reply_markup=kb_live_paywall(match_id),
                )
                return

        # -------- Контент --------
        raw = await call_agent_local(tg_user_id, f"ui match {match_id} {mode} {action}")

        title = _get_match_title(context, match_id)
        if mode == "pre" and action == "overview":
            head = "📊 Обзор рынков"
        elif mode == "live" and action == "overview":
            head = "🟢 LIVE-обзор"
        elif mode == "live" and action == "refresh":
            head = "🔄 LIVE обновление"
        else:
            head = "🧠 Разбор"

        if title:
            head = f"{head}\n{title}"

        text_out = _wrap_screen(head, raw, footer=True)
        await _edit_or_send(screen_msg, text_out, reply_markup=kb_match_hub(match_id))
        return

    if data == "PAY:PREMIUM":
        await _edit_or_send(
            screen_msg,
            (
                "⭐ Premium (скоро)\n\n"
                "Оплата будет подключена (Telegram Payments / YooKassa).\n"
                "Пока можно активировать вручную через админ-команду.\n\n"
                "Если хочешь — добавим команду активации для теста."
            ),
            reply_markup=kb_premium(),
        )
        return

    await _edit_or_send(screen_msg, "Не понял действие 🤔", reply_markup=MAIN_KB)


# -----------------------------
# Errors handler
# -----------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled telegram error: %s", context.error)


# -----------------------------
# Application factory
# -----------------------------
def build_telegram_application() -> Application:
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("help", help_cmd))
    tg_app.add_handler(CommandHandler("premium", premium_cmd))
    tg_app.add_handler(CallbackQueryHandler(handle_callback))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    tg_app.add_error_handler(error_handler)  # важно, чтобы не было "No error handlers"
    return tg_app


# -----------------------------
# FastAPI mount (webhook-only)
# -----------------------------
def mount(fastapi_app: FastAPI) -> None:
    """
    Регистрирует webhook endpoint и lifecycle-хуки.
    НИКАКОГО polling.
    """
    tg_app = build_telegram_application()
    fastapi_app.state.telegram_app = tg_app
    fastapi_app.state.telegram_ready = False  # защита от ранних webhook

    async def _startup() -> None:
        await tg_app.initialize()
        await tg_app.start()
        fastapi_app.state.telegram_ready = True

        if not PUBLIC_URL:
            logger.warning("PUBLIC_URL is not set -> webhook will NOT be configured automatically.")
            return

        webhook_url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
        try:
            await tg_app.bot.delete_webhook(drop_pending_updates=True)
            await tg_app.bot.set_webhook(
                url=webhook_url,
                secret_token=WEBHOOK_SECRET or None,
                drop_pending_updates=True,
            )
            logger.info("Telegram webhook set: %s", webhook_url)
        except Exception as e:
            logger.exception("Failed to set telegram webhook: %s", e)

    async def _shutdown() -> None:
        fastapi_app.state.telegram_ready = False
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

        if not getattr(fastapi_app.state, "telegram_ready", False):
            raise HTTPException(status_code=503, detail="Telegram app is starting")

        payload = await request.json()
        update = Update.de_json(payload, tg_app.bot)
        await tg_app.process_update(update)
        return {"ok": True}
