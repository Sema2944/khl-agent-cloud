# src/telegram_bot.py

import os
import logging
import asyncio
import re
from datetime import datetime

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
import httpx


API_BASE = os.getenv("API_BASE", "https://khl-agent-api.onrender.com")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def build_main_keyboard() -> ReplyKeyboardMarkup:
    """
    Главная клавиатура с основными кнопками.
    Тексты кнопок совпадают с командами, которые понимает run_agent.
    """
    keyboard = [
        ["профиль", "мои ставки"],
        ["КХЛ сегодня", "отчёт за неделю"],
        ["разбор моих рынков", "состояние банка"],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


def build_bet_result_keyboard(bet_id: int) -> InlineKeyboardMarkup:
    """
    Инлайн-клавиатура под ставкой:
    🟢 Выиграла / 🔴 Проиграла / ⚪️ Возврат
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🟢 Выиграла", callback_data=f"BET_RES:{bet_id}:win"
                ),
                InlineKeyboardButton(
                    "🔴 Проиграла", callback_data=f"BET_RES:{bet_id}:lose"
                ),
            ],
            [
                InlineKeyboardButton(
                    "⚪️ Возврат", callback_data=f"BET_RES:{bet_id}:push"
                ),
            ],
        ]
    )


async def call_agent(user_id: int, message: str) -> str:
    """
    Шлём запрос в твой /agent/query и возвращаем текст ответа.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{API_BASE}/agent/query",
            json={"user_id": user_id, "message": message},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("reply", "Пустой ответ от агента 😕")


async def call_last_bets(user_id: int, limit: int = 5) -> list[dict]:
    """
    Вызываем /agent/last-bets и возвращаем список словарей со ставками.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{API_BASE}/agent/last-bets",
            params={"user_id": user_id, "limit": limit},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("bets", []) or []


# ---------- ПРОСТОЙ ПИНГ, ЧТОБЫ ПРОВЕРИТЬ, ЖИВ ЛИ БОТ ----------

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Диагностическая команда: не трогает сервер, просто отвечает, что бот жив.
    """
    if not update.message:
        return

    await update.message.reply_text(
        "✅ Я на связи. Это ответ напрямую от Telegram-бота.\n"
        "Если другие команды молчат — значит, проблема в сервере /agent, а не в боте."
    )


# ------------------- /start -------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start — приветственное сообщение + показ клавиатуры.
    """
    if not update.message:
        return

    text = (
        "Привет! Я AI-агент для ставок на хоккей 🏒\n\n"
        "Я умею:\n"
        "• Вести историю ставок и статистику (winrate, ROI, PnL)\n"
        "• Работать с банк-менеджментом\n"
        "• Делать отчёты за неделю и разбор твоих рынков\n"
        "• Показывать матчи КХЛ на сегодня и делать разбор матча\n\n"
        "Нажимай на кнопки внизу или напиши мне что-нибудь 😉"
    )

    await update.message.reply_text(
        text,
        reply_markup=build_main_keyboard(),
    )


def _format_bet_for_user(b: dict) -> str:
    """
    Формируем человекочитаемый текст ставки для 'мои ставки'.
    Ожидаем поля из /agent/last-bets.
    """
    bet_id = b.get("id")
    created_raw = b.get("created_at")
    event = b.get("event")
    outcome = b.get("outcome")
    stake = b.get("stake")
    odds = b.get("odds")
    result = b.get("result")
    profit = b.get("profit")

    # Дата/время
    dt_str = ""
    if created_raw:
        try:
            dt = datetime.fromisoformat(created_raw)
            dt_str = dt.strftime("%d.%m %H:%M")
        except Exception:
            dt_str = created_raw

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
        lines.append(f"Сумма: {stake:.0f}")
    if odds is not None:
        lines.append(f"Коэффициент: {odds:.2f}")

    if result:
        # человекочитаемый результат
        if result == "win":
            human = "выигрыш"
        elif result == "lose":
            human = "проигрыш"
        elif result == "push":
            human = "возврат"
        else:
            human = result
        res_line = f"Результат: {human}"
        if profit is not None:
            sign = "+" if profit >= 0 else ""
            res_line += f", PnL: {sign}{profit:.0f}"
        lines.append(res_line)

    return "\n".join(lines)


# ------------------- ОБРАБОТКА ТЕКСТОВ -------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатываем любое текстовое сообщение:
    → если это 'мои ставки' — идём в /agent/last-bets и шлём красивый список с кнопками.
    → иначе отправляем текст на бекенд-агент и отвечаем как раньше.
    """
    if not update.message:
        return

    user_id = update.effective_user.id
    text = update.message.text or ""
    norm = text.strip().lower()

    logger.info("handle_message: user_id=%s, text=%r", user_id, text)

    # Особый случай: "мои ставки" — забираем структурированные данные и рисуем сами
    if "мои ставки" in norm:
        try:
            bets = await call_last_bets(user_id, limit=5)
        except Exception as e:
            logger.exception("Ошибка при вызове /agent/last-bets: %s", e)
            await update.message.reply_text(
                "Не удалось получить список ставок 😔\n"
                "Попробуй ещё раз чуть позже.",
                reply_markup=build_main_keyboard(),
            )
            return

        if not bets:
            await update.message.reply_text(
                "У тебя пока нет сохранённых ставок.",
                reply_markup=build_main_keyboard(),
            )
            return

        # Первое сообщение — заголовок + клавиатура
        await update.message.reply_text(
            "Твои последние ставки:",
            reply_markup=build_main_keyboard(),
        )

        # По одной ставке в сообщение, под незакрытыми — кнопки результата
        for b in bets:
            msg_text = _format_bet_for_user(b)
            result = b.get("result")
            bet_id = b.get("id")

            if result is None and bet_id is not None:
                # ставка ещё не рассчитана — показываем кнопки
                await update.message.reply_text(
                    msg_text,
                    reply_markup=build_bet_result_keyboard(bet_id),
                )
            else:
                # уже рассчитана — просто текст
                await update.message.reply_text(msg_text)

        return

    # Обычный путь: шлём текст в /agent/query
    try:
        reply = await call_agent(user_id, text)
    except Exception as e:
        logger.exception("Ошибка при вызове агента: %s", e)
        reply = (
            "Не удалось связаться с сервером агента 😔\n"
            "Бот жив, но бэкенд временно недоступен.\n"
            "Попробуй ещё раз чуть позже или используй команды, не завязанные на сервер."
        )
        await update.message.reply_text(reply, reply_markup=build_main_keyboard())
        return

    # Пытаемся вытащить id ставки из ответа вида: "Ставка сохранена (id: 3)."
    m_bet = re.search(r"Ставка сохранена \(id:\s*(\d+)\)", reply)
    if m_bet:
        bet_id = int(m_bet.group(1))
        # Отправляем ответ с инлайн-кнопками для отметки результата
        await update.message.reply_text(
            reply,
            reply_markup=build_bet_result_keyboard(bet_id),
        )
        return

    # Если пользователь просит меню/помощь — показываем клавиатуру
    if norm in {"/start", "start", "меню", "help", "/help"}:
        await update.message.reply_text(reply, reply_markup=build_main_keyboard())
    else:
        # Обычный ответ без изменения клавиатуры
        await update.message.reply_text(reply)


# ------------------- CALLBACK-КНОПКИ -------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработка нажатий по инлайн-кнопкам.
    Сейчас поддерживаем только BET_RES:<bet_id>:<win/lose/push>.
    """
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    await query.answer()  # убираем "часики" у кнопки

    if not data.startswith("BET_RES:"):
        # На будущее: можно обрабатывать другие типы callback_data
        return

    try:
        _, bet_id_str, res_code = data.split(":", 2)
        bet_id = int(bet_id_str)
    except Exception:
        logger.warning("Некорректный callback_data: %s", data)
        return

    user_id = query.from_user.id

    if res_code == "win":
        cmd_text = f"ставка {bet_id} выиграла"
        status_label = "выигрыш"
    elif res_code == "lose":
        cmd_text = f"ставка {bet_id} проиграла"
        status_label = "проигрыш"
    else:
        cmd_text = f"ставка {bet_id} возврат"
        status_label = "возврат"

    # Дёргаем бекенд так же, как если бы пользователь написал текстом
    try:
        agent_reply = await call_agent(user_id, cmd_text)
    except Exception as e:
        logger.exception("Ошибка при отметке результата ставки через callback: %s", e)
        agent_reply = (
            "Не удалось отметить результат ставки на сервере 😔\n"
            "Попробуй ещё раз или введи текстом: "
            f"'{cmd_text}'."
        )

    # Обновляем исходное сообщение: добавляем инфу, убираем кнопки
    try:
        original_text = query.message.text or ""
        new_text = original_text + f"\n\n✅ Результат отмечен: {status_label}."
        await query.edit_message_text(new_text)
    except Exception as e:
        logger.warning("Не удалось отредактировать сообщение с кнопками: %s", e)

    # И шлём подробный ответ от агента отдельным сообщением
    try:
        await query.message.reply_text(agent_reply)
    except Exception as e:
        logger.warning("Не удалось отправить ответ после callback: %s", e)


# ------------------- MAIN -------------------

def main() -> None:
    """
    Точка входа бота.

    ВАЖНО:
    - бот запускается в отдельном потоке (из FastAPI),
    - поэтому мы создаём свой asyncio event loop,
    - и избегаем установки signal handlers (stop_signals=None),
      иначе Python ругается `set_wakeup_fd only works in main thread`.
    """
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в переменных окружения")

    logger.info("Запускаю Telegram-бота...")

    # создаём event loop для текущего (фонового) потока
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(BOT_TOKEN).build()

    # Команда /start
    app.add_handler(CommandHandler("start", start))
    # Диагностический /ping
    app.add_handler(CommandHandler("ping", ping))
    # Обработка callback-кнопок
    app.add_handler(CallbackQueryHandler(handle_callback))
    # Все текстовые сообщения (кроме команд) — в агент / спец-обработчики
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling(stop_signals=None)
