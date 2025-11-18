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


def extract_stake_id_from_reply(reply: str) -> int | None:
    """
    Парсим id ставки из текста ответа вида:
    'Ставка сохранена (id: 3).'
    """
    m = re.search(r"id:\s*(\d+)\)", reply)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def build_stake_result_keyboard(stake_id: int) -> InlineKeyboardMarkup:
    """
    Инлайн-клавиатура для быстрого проставления результата ставки.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Выиграла", callback_data=f"bet_result:{stake_id}:win"
                ),
                InlineKeyboardButton(
                    "❌ Проиграла", callback_data=f"bet_result:{stake_id}:lose"
                ),
            ],
            [
                InlineKeyboardButton(
                    "↔️ Возврат", callback_data=f"bet_result:{stake_id}:push"
                ),
            ],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start — приветственное сообщение + показ клавиатуры.
    """
    if not update.message:
        return

    text = (
        "Привет! Я AI-агент для ставок на спорт.\n\n"
        "Я умею:\n"
        "• Вести историю ставок и статистику (winrate, ROI, PnL)\n"
        "• Работать с банк-менеджментом\n"
        "• Делать отчёты за неделю и разбор твоих рынков\n"
        "• Показывать матчи КХЛ на сегодня и делать базовый разбор матча\n\n"
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

    Если это ответ про 'Ставка сохранена (id: N)',
    добавляем инлайн-кнопки: Выиграла / Проиграла / Возврат
    и подчищаем старый текст про 'напиши, например...'.
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

    # Если пользователь просит меню/помощь — показываем клавиатуру
    if norm in {"/start", "start", "меню", "help", "/help"}:
        await update.message.reply_text(reply, reply_markup=build_main_keyboard())
        return

    # Пытаемся понять, что это ответ про сохранённую ставку
    stake_id = None
    if reply.startswith("Ставка сохранена"):
        stake_id = extract_stake_id_from_reply(reply)

    # Если нашли id ставки — добавляем инлайн-кнопки для результата
    if stake_id is not None:
        # Чистим хвост "Когда узнаешь результат, напиши, например: ..."
        marker = "Когда узнаешь результат, напиши, например:"
        pos = reply.find(marker)
        if pos != -1:
            reply = reply[:pos].rstrip()
            reply += (
                "\n\nКогда матч закончится — просто нажми кнопку ниже, "
                "чтобы отметить результат ставки. 👇"
            )

        keyboard = build_stake_result_keyboard(stake_id)
        await update.message.reply_text(reply, reply_markup=keyboard)
    else:
        # Обычный ответ без доп. клавиатуры
        await update.message.reply_text(reply)


async def handle_bet_result_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Обработка нажатий по инлайн-кнопкам:
    bet_result:<stake_id>:<win|lose|push>

    Мы НЕ лезем напрямую в базу, а просто
    шлём в /agent/query фразу вроде:
    'ставка 3 выиграла', чтобы сработала
    уже существующая логика settle_bet.
    """
    query = update.callback_query
    if not query:
        return

    await query.answer()

    data = query.data or ""
    parts = data.split(":")
    if len(parts) != 3:
        await query.message.reply_text("Не понял действие по ставке.")
        return

    _, stake_id_str, result_code = parts

    try:
        stake_id = int(stake_id_str)
    except ValueError:
        await query.message.reply_text("Некорректный id ставки.")
        return

    # Маппим код на русское слово, которое уже понимает run_agent
    if result_code == "win":
        res_word = "выиграла"
    elif result_code == "lose":
        res_word = "проиграла"
    elif result_code == "push":
        res_word = "возврат"
    else:
        await query.message.reply_text("Неизвестный результат ставки.")
        return

    user_id = query.from_user.id

    try:
        # Прокидываем в /agent/query как обычный текст
        agent_message = f"ставка {stake_id} {res_word}"
        reply = await call_agent(user_id, agent_message)
    except Exception as e:
        logger.exception("Ошибка при применении результата ставки: %s", e)
        await query.message.reply_text(
            "Не удалось обновить результат ставки. Попробуй позже."
        )
        return

    # Убираем инлайн-кнопки у исходного сообщения
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        # Если вдруг не получилось отредактировать (старое сообщение и т.п.) — просто игнорим
        pass

    # Шлём ответ от агента (там уже и банк, и PnL, и подсказки)
    await query.message.reply_text(reply)


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
    # Callback-и (инлайн-кнопки по ставкам)
    app.add_handler(
        CallbackQueryHandler(handle_bet_result_callback, pattern=r"^bet_result:")
    )
    # Все текстовые сообщения (кроме команд) — в агент
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Блокирующий запуск polling в этом потоке,
    # без установки signal handlers (stop_signals=None)
    app.run_polling(stop_signals=None)
