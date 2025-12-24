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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
API_BASE = (os.getenv("API_BASE") or "").strip().rstrip("/")
BACKEND_TIMEOUT = float((os.getenv("BACKEND_TIMEOUT") or "8").strip())

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

if not API_BASE:
    logger.warning("API_BASE is not set. Bot will work in 'no-backend' mode.")

# --- Главное меню (одна клавиатура) ---
MAIN_KB = ReplyKeyboardMarkup(
    [
        ["🧠 AI Аналитика", "👤 Стратегия эксперта"],
        ["🏒 КХЛ сегодня", "📊 Профиль"],
        ["📒 Мои ставки", "📆 Отчёт за неделю"],
        ["📉 Разбор моих рынков", "🏦 Состояние банка"],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)

# --- Inline под матчем ---
def build_match_actions_keyboard(match_id: str) -> InlineKeyboardMarkup:
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


def build_bet_result_keyboard(bet_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🟢 Выиграла", callback_data=f"BET_RES:{bet_id}:win"),
                InlineKeyboardButton("🔴 Проиграла", callback_data=f"BET_RES:{bet_id}:lose"),
            ],
            [InlineKeyboardButton("⚪️ Возврат", callback_data=f"BET_RES:{bet_id}:push")],
        ]
    )


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


def _format_bet_for_user(b: dict) -> str:
    bet_id = b.get("id")
    created_raw = b.get("created_at")
    event = b.get("event")
    outcome = b.get("outcome")
    stake = b.get("stake")
    odds = b.get("odds")
    result = b.get("result")
    profit = b.get("profit")

    dt_str = ""
    if created_raw:
        try:
            dt = datetime.fromisoformat(created_raw)
            dt_str = dt.strftime("%d.%m %H:%M")
        except Exception:
            dt_str = str(created_raw)

    lines: list[str] = []
    header = f"Ставка #{bet_id}"
    if dt_str:
        header += f" от {dt_str}"
    lines.append(header)

    if event:
        lines.append(f"Событие: {event}")
    if outcome:
        lines.append(f"Исход: {outcome}")

    if stake is not None:
        try:
            lines.append(f"Сумма: {float(stake):.0f}")
        except Exception:
            lines.append(f"Сумма: {stake}")

    if odds is not None:
        try:
            lines.append(f"Коэффициент: {float(odds):.2f}")
        except Exception:
            lines.append(f"Коэффициент: {odds}")

    if result:
        mapping = {"win": "выигрыш", "lose": "проигрыш", "push": "возврат"}
        human = mapping.get(result, result)
        line = f"Результат: {human}"
        if profit is not None:
            try:
                p = float(profit)
                sign = "+" if p >= 0 else ""
                line += f", PnL: {sign}{p:.0f}"
            except Exception:
                line += f", PnL: {profit}"
        lines.append(line)

    return "\n".join(lines)


def _normalize_menu_text(text: str) -> str:
    t = (text or "").strip().lower()
    # убираем эмодзи для сравнения
    t = re.sub(r"[^\w\sа-яё-]", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "✅ Я на связи.\n\nВыбирай действие кнопками ниже.",
            reply_markup=MAIN_KB,
        )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    await update.message.reply_text(msg, reply_markup=MAIN_KB)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    user_id = update.effective_user.id
    text = update.message.text or ""
    norm = _normalize_menu_text(text)

    logger.info("handle_message user_id=%s text=%r", user_id, text)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # --- Меню: стратегия / аналитика ---
    if norm in {"стратегия эксперта", "стратегия", "эксперт", "эксперт сегодня"}:
        try:
            reply = await call_agent(user_id, "стратегия")
        except Exception:
            await update.message.reply_text("Backend недоступен 😔", reply_markup=MAIN_KB)
            return
        await update.message.reply_text(reply, reply_markup=MAIN_KB)
        return

    if norm in {"ai аналитика", "аналитика", "ии аналитика"}:
        await update.message.reply_text(
            "Напиши в формате:\n`аналитика <id матча>` или `аналитика <вопрос>`",
            reply_markup=MAIN_KB,
        )
        return

    # --- Мои ставки ---
    if norm in {"мои ставки"}:
        try:
            bets = await call_last_bets(user_id, 5)
        except Exception:
            await update.message.reply_text("Backend недоступен 😔", reply_markup=MAIN_KB)
            return

        if not bets:
            await update.message.reply_text("У тебя нет сохранённых ставок.", reply_markup=MAIN_KB)
            return

        await update.message.reply_text("Твои последние ставки:", reply_markup=MAIN_KB)
        for b in bets:
            msg = _format_bet_for_user(b)
            if b.get("result") is None and b.get("id") is not None:
                await update.message.reply_text(msg, reply_markup=build_bet_result_keyboard(int(b["id"])))
            else:
                await update.message.reply_text(msg, reply_markup=MAIN_KB)
        return

    # --- КХЛ сегодня (плюс inline под матчем) ---
    if norm in {"кхл сегодня"}:
        try:
            reply = await call_agent(user_id, "кхл сегодня")
        except Exception:
            await update.message.reply_text("Backend недоступен 😔\nПопробуй позже.", reply_markup=MAIN_KB)
            return

        # Попробуем вытащить id матча из ответа (id: XXXXX)
        m = re.search(r"id:\s*([a-zA-Z0-9_\-:.]{4,80})", reply)
        if m:
            match_id = m.group(1)
            await update.message.reply_text(reply, reply_markup=build_match_actions_keyboard(match_id))
            # отдельным сообщением закрепим главное меню, чтобы оно не исчезало
            await update.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
            return

        await update.message.reply_text(reply, reply_markup=MAIN_KB)
        return

    # --- Остальные кнопки меню мапим на старые команды ---
    mapping = {
        "профиль": "профиль",
        "отчёт за неделю": "отчёт за неделю",
        "отчет за неделю": "отчет за неделю",
        "состояние банка": "состояние банка",
        "разбор моих рынков": "разбор моих рынков",
    }
    if norm in mapping:
        try:
            reply = await call_agent(user_id, mapping[norm])
        except Exception:
            await update.message.reply_text("Backend недоступен 😔\nПопробуй позже.", reply_markup=MAIN_KB)
            return
        await update.message.reply_text(reply, reply_markup=MAIN_KB)
        return

    # --- Всё остальное отдаём агенту как есть ---
    try:
        reply = await call_agent(user_id, text)
    except Exception:
        await update.message.reply_text("Backend недоступен 😔\nПопробуй позже.", reply_markup=MAIN_KB)
        return

    # если создали ставку — покажем inline-кнопки результата
    m = re.search(r"Ставка сохранена \(id:\s*(\d+)\)", reply)
    if m:
        bet_id = int(m.group(1))
        await update.message.reply_text(reply, reply_markup=build_bet_result_keyboard(bet_id))
        # и снова меню
        await update.message.reply_text("Меню ниже 👇", reply_markup=MAIN_KB)
        return

    await update.message.reply_text(reply, reply_markup=MAIN_KB)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    await query.answer()

    # --- Inline под матчем ---
    if data.startswith("MATCH_LINE:"):
        match_id = data.split(":", 1)[1]
        try:
            text = await call_agent(query.from_user.id, f"линия {match_id}")
        except Exception:
            text = "Backend недоступен 😔"
        await query.message.reply_text(text, reply_markup=MAIN_KB)
        return

    if data.startswith("MATCH_AI:"):
        match_id = data.split(":", 1)[1]
        try:
            text = await call_agent(query.from_user.id, f"аналитика {match_id}")
        except Exception:
            text = "Backend недоступен 😔"
        await query.message.reply_text(text, reply_markup=MAIN_KB)
        return

    if data.startswith("MATCH_EXPERT:"):
        # MVP: просто показываем стратегию на сегодня
        try:
            text = await call_agent(query.from_user.id, "стратегия")
        except Exception:
            text = "Backend недоступен 😔"
        await query.message.reply_text(text, reply_markup=MAIN_KB)
        return

    # --- Inline по ставке ---
    if data.startswith("BET_RES:"):
        _, bet_id_str, res = data.split(":")
        bet_id = int(bet_id_str)
        user_id = query.from_user.id

        mapping = {"win": "выигрыш", "lose": "проигрыш", "push": "возврат"}
        cmd = {
            "win": f"ставка {bet_id} выиграла",
            "lose": f"ставка {bet_id} проиграла",
            "push": f"ставка {bet_id} возврат",
        }[res]

        try:
            agent_reply = await call_agent(user_id, cmd)
        except Exception:
            agent_reply = "Backend недоступен 😔"

        original = query.message.text or ""
        new_text = original + f"\n\n✅ Результат отмечен: {mapping[res]}."
        await query.edit_message_text(new_text)
        await query.message.reply_text(agent_reply, reply_markup=MAIN_KB)
        return


def main() -> None:
    logger.info("Starting Telegram bot. API_BASE=%r TIMEOUT=%s", API_BASE, BACKEND_TIMEOUT)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(stop_signals=None)


if __name__ == "__main__":
    main()
