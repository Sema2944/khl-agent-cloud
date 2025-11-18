# src/telegram_bot.py

import os
import logging
import asyncio
import re

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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатываем любое текстовое сообщение:
    → отправляем его на бекенд-агент
    → возвращаем ответ пользователю.
    Если это ответ вида 'Ставка сохранена (id: X)...' — добавляем инлайн-кнопки.
    """
    if not update.message:
        return

    user_id = update.effective_user.id
    text = update.message.text or ""
    norm = text.strip().lower()

    try:
        reply = await call_agent(user_id, text)
    except Exception as e:
        logger.exception("Ошибка при вызове агента: %s", e)
        reply = (
            "Не удалось связаться с сервером агента 😔\n"
            "Попробуй ещё раз чуть позже."
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
    # Обработка callback-кнопок
    app.add_handler(CallbackQueryHandler(handle_callback))
    # Все текстовые сообщения (кроме команд) — в агент
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Блокирующий запуск polling в этом потоке,
    # без установки signal handlers (stop_signals=None)
    app.run_polling(stop_signals=None)
