# src/telegram_bot/bot.py
from __future__ import annotations

import os
import logging
import asyncio
import re
from datetime import datetime

import httpx
from telegram import (
    Update,
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
    ConversationHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
API_BASE = (os.getenv("API_BASE") or "").strip().rstrip("/")
BACKEND_TIMEOUT = float((os.getenv("BACKEND_TIMEOUT") or "10").strip())

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

if not API_BASE:
    logger.warning("API_BASE is not set. Bot will work in 'no-backend' mode.")

# -----------------------------
# FSM states
# -----------------------------
SPORT, LEAGUE, MATCH, ACTION = range(4)

# -----------------------------
# Helpers: backend calls
# -----------------------------
async def _safe_request(method: str, url: str, **kwargs) -> dict:
    timeout = kwargs.pop("timeout", BACKEND_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.request(method, url, **kwargs)
        r.raise_for_status()
        return r.json()

async def backend_health() -> tuple[bool, str]:
    if not API_BASE:
        return False, "API_BASE пуст (backend не настроен)."
    try:
        data = await _safe_request("GET", f"{API_BASE}/", timeout=min(BACKEND_TIMEOUT, 6.0))
        status = str(data.get("status", "")).lower()
        if status == "ok":
            return True, f"OK: {data}"
        return False, f"Backend ответил, но статус не ok: {data}"
    except httpx.TimeoutException:
        return False, f"timeout > {BACKEND_TIMEOUT}s"
    except httpx.HTTPStatusError as e:
        return False, f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    except httpx.RequestError as e:
        return False, f"RequestError: {str(e)}"

async def call_agent(user_id: int, message: str) -> str:
    if not API_BASE:
        return "Backend не настроен (API_BASE пуст). Проверь переменные окружения на Render."
    payload = {"user_id": user_id, "message": message}
    data = await _safe_request("POST", f"{API_BASE}/agent/query", json=payload, timeout=BACKEND_TIMEOUT)
    return data.get("reply", "Пустой ответ от сервера 😕")

# -----------------------------
# UI keyboards
# -----------------------------
def kb_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏒 Хоккей", callback_data="SPORT:hockey")],
            [InlineKeyboardButton("👤 Стратегия эксперта на сегодня", callback_data="EXPERT:today")],
            [InlineKeyboardButton("📊 Профиль", callback_data="CMD:profile"),
             InlineKeyboardButton("🧾 Мои ставки", callback_data="CMD:mybets")],
            [InlineKeyboardButton("📆 Отчёт за неделю", callback_data="CMD:week"),
             InlineKeyboardButton("🏦 Состояние банка", callback_data="CMD:bank")],
        ]
    )

def kb_leagues_hockey() -> InlineKeyboardMarkup:
    # MVP: пока только КХЛ кнопкой, позже добавим НХЛ и др.
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("КХЛ (сегодня)", callback_data="LEAGUE:khl")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="BACK:main")],
        ]
    )

def kb_matches(matches: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for m in matches[:10]:
        mid = str(m.get("id") or "")
        title = str(m.get("title") or m.get("name") or mid or "match")
        if not mid:
            continue
        rows.append([InlineKeyboardButton(title, callback_data=f"MATCH:{mid}")])

    rows.append([InlineKeyboardButton("🔄 Обновить", callback_data="REFRESH:matches")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="BACK:league")])
    return InlineKeyboardMarkup(rows)

def kb_match_actions(match_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🧠 AI аналитика", callback_data=f"AI:{match_id}")],
            [InlineKeyboardButton("📈 Линия / коэффициенты", callback_data=f"LINE:{match_id}")],
            [InlineKeyboardButton("👤 Стратегия эксперта на сегодня", callback_data="EXPERT:today")],
            [InlineKeyboardButton("⬅️ Назад к матчам", callback_data="BACK:matches")],
            [InlineKeyboardButton("🏠 В меню", callback_data="BACK:main")],
        ]
    )

# -----------------------------
# Parsing helpers (from replies)
# -----------------------------
def parse_matches_from_text(text: str) -> list[dict]:
    """
    Парсим простой формат, который уже выдаёт backend:
    "1) СКА — ЦСКА (id: demo_khl_123456)"
    """
    matches: list[dict] = []
    for line in (text or "").splitlines():
        m = re.search(r"\(id:\s*([^)]+)\)", line)
        if not m:
            continue
        match_id = m.group(1).strip()
        # title = строка до "(id:"
        title = line.split("(id:", 1)[0].strip()
        title = re.sub(r"^\d+\)\s*", "", title).strip()
        if match_id:
            matches.append({"id": match_id, "title": title or match_id})
    return matches

# -----------------------------
# Commands
# -----------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END

    await update.message.reply_text(
        "✅ Меню открыто.\n\nВыбери действие:",
        reply_markup=kb_main_menu(),
    )
    context.user_data.clear()
    return SPORT

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # alias to /start
    return await cmd_start(update, context)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    ok, info = await backend_health()
    msg = (
        "✅ Бот жив.\n"
        f"API_BASE: {API_BASE or '—'}\n"
        f"TIMEOUT: {BACKEND_TIMEOUT}s\n"
        f"Backend: {'OK' if ok else 'FAIL'}\n"
        f"Info: {info}"
    )
    await update.message.reply_text(msg, reply_markup=kb_main_menu())

# -----------------------------
# FSM handlers: callbacks
# -----------------------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return SPORT

    await query.answer()
    data = query.data or ""
    user_id = query.from_user.id

    # unified "typing"
    try:
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    # BACK navigation
    if data.startswith("BACK:"):
        where = data.split(":", 1)[1]
        if where == "main":
            context.user_data.clear()
            await query.edit_message_text("🏠 Главное меню:", reply_markup=kb_main_menu())
            return SPORT
        if where == "league":
            await query.edit_message_text("Выбери лигу:", reply_markup=kb_leagues_hockey())
            return LEAGUE
        if where == "matches":
            # show cached matches
            matches = context.user_data.get("matches") or []
            if not matches:
                await query.edit_message_text("Матчи не загружены. Нажми КХЛ ещё раз.", reply_markup=kb_leagues_hockey())
                return LEAGUE
            await query.edit_message_text("Выбери матч:", reply_markup=kb_matches(matches))
            return MATCH

    # COMMANDS from menu
    if data.startswith("CMD:"):
        cmd = data.split(":", 1)[1]
        mapping = {
            "profile": "профиль",
            "mybets": "мои ставки",
            "week": "отчёт за неделю",
            "bank": "состояние банка",
        }
        text_cmd = mapping.get(cmd)
        if not text_cmd:
            return SPORT

        try:
            reply = await call_agent(user_id, text_cmd)
        except Exception:
            reply = "Backend недоступен 😔\nПопробуй позже."

        await query.edit_message_text(reply, reply_markup=kb_main_menu())
        return SPORT

    # Expert strategy
    if data == "EXPERT:today":
        try:
            reply = await call_agent(user_id, "стратегия")
        except Exception:
            reply = "Backend недоступен 😔\nПопробуй позже."
        await query.edit_message_text(reply, reply_markup=kb_main_menu())
        return SPORT

    # Sport selection
    if data.startswith("SPORT:"):
        sport = data.split(":", 1)[1]
        context.user_data["sport"] = sport
        if sport == "hockey":
            await query.edit_message_text("Выбери лигу:", reply_markup=kb_leagues_hockey())
            return LEAGUE
        await query.edit_message_text("Этот спорт будет добавлен позже.", reply_markup=kb_main_menu())
        return SPORT

    # League selection
    if data.startswith("LEAGUE:"):
        league = data.split(":", 1)[1]
        context.user_data["league"] = league

        # MVP: КХЛ сегодня — получаем список матчей через existing команду
        if league == "khl":
            try:
                text = await call_agent(user_id, "кхл сегодня")
                matches = parse_matches_from_text(text)
                context.user_data["matches"] = matches
            except Exception:
                await query.edit_message_text("Backend недоступен 😔\nПопробуй позже.", reply_markup=kb_main_menu())
                return SPORT

            if not context.user_data.get("matches"):
                await query.edit_message_text(
                    "Не нашёл матчи на сегодня. Попробуй позже.",
                    reply_markup=kb_leagues_hockey(),
                )
                return LEAGUE

            await query.edit_message_text("Выбери матч:", reply_markup=kb_matches(context.user_data["matches"]))
            return MATCH

        await query.edit_message_text("Эта лига будет добавлена позже.", reply_markup=kb_leagues_hockey())
        return LEAGUE

    # Refresh matches
    if data == "REFRESH:matches":
        league = context.user_data.get("league")
        if league == "khl":
            try:
                text = await call_agent(user_id, "кхл сегодня")
                matches = parse_matches_from_text(text)
                context.user_data["matches"] = matches
            except Exception:
                await query.edit_message_text("Backend недоступен 😔\nПопробуй позже.", reply_markup=kb_main_menu())
                return SPORT

            if not matches:
                await query.edit_message_text("Пока нет матчей. Попробуй позже.", reply_markup=kb_leagues_hockey())
                return LEAGUE

            await query.edit_message_text("Выбери матч:", reply_markup=kb_matches(matches))
            return MATCH

    # Match selection
    if data.startswith("MATCH:"):
        match_id = data.split(":", 1)[1]
        context.user_data["match_id"] = match_id

        # показываем действия
        await query.edit_message_text(
            f"Матч выбран: `{match_id}`\n\nЧто делаем?",
            reply_markup=kb_match_actions(match_id),
            parse_mode="Markdown",
        )
        return ACTION

    # AI analysis
    if data.startswith("AI:"):
        match_id = data.split(":", 1)[1]
        try:
            reply = await call_agent(user_id, f"аналитика {match_id}")
        except Exception:
            reply = "Backend недоступен 😔\nПопробуй позже."

        await query.edit_message_text(reply, reply_markup=kb_match_actions(match_id))
        return ACTION

    # Line
    if data.startswith("LINE:"):
        match_id = data.split(":", 1)[1]
        try:
            reply = await call_agent(user_id, f"линия {match_id}")
        except Exception:
            reply = "Backend недоступен 😔\nПопробуй позже."

        await query.edit_message_text(reply, reply_markup=kb_match_actions(match_id))
        return ACTION

    # default fallthrough
    return SPORT

# -----------------------------
# Text handler (fallback: still works)
# -----------------------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Оставляем текстовый ввод как fallback (параллельно FSM).
    """
    if not update.message:
        return SPORT

    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    if not text:
        return SPORT

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # быстрый хелп для людей
    if text.lower() in ("/menu", "меню"):
        await update.message.reply_text("🏠 Главное меню:", reply_markup=kb_main_menu())
        return SPORT

    try:
        reply = await call_agent(user_id, text)
    except Exception:
        reply = "Backend недоступен 😔\nПопробуй позже."

    await update.message.reply_text(reply, reply_markup=kb_main_menu())
    return SPORT

# -----------------------------
# Entry point
# -----------------------------
def main() -> None:
    logger.info("Starting Telegram bot. API_BASE=%r TIMEOUT=%s", API_BASE, BACKEND_TIMEOUT)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(BOT_TOKEN).build()

    # ConversationHandler держит FSM на кнопках
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start), CommandHandler("menu", cmd_menu)],
        states={
            SPORT: [CallbackQueryHandler(on_callback), MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)],
            LEAGUE: [CallbackQueryHandler(on_callback), MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)],
            MATCH: [CallbackQueryHandler(on_callback), MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)],
            ACTION: [CallbackQueryHandler(on_callback), MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)],
        },
        fallbacks=[CommandHandler("start", cmd_start), CommandHandler("menu", cmd_menu)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("ping", cmd_ping))

    # На всякий случай: если кто-то пишет вне стейта
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling(stop_signals=None)

if __name__ == "__main__":
    main()
