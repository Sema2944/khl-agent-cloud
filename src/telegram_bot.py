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


# ==============================
# Инлайн-клавиатура под разбором матча
# ==============================

async def send_match_analysis(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    analysis_text: str,
    match_id: str,
) -> None:
    """
    Отправляем разбор матча + инлайн-кнопки:
    - value-разбор
    - полный разбор
    - добавить ставку
    """
    keyboard = [
        [
            InlineKeyboardButton("🔍 Value-проверка", callback_data=f"value_{match_id}"),
            InlineKeyboardButton("📊 Полный разбор", callback_data=f"deep_{match_id}"),
        ],
        [
            InlineKeyboardButton("➕ Добавить ставку", callback_data=f"addbet_{match_id}"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(analysis_text, reply_markup=reply_markup)
    elif update.callback_query and update.callback_query.message:
        # на всякий случай, если когда-нибудь будем вызывать из callback
        await update.callback_query.message.reply_text(
            analysis_text, reply_markup=reply_markup
        )


# ==============================
# Обработка нажатий по инлайн-кнопкам
# ==============================

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработка callback_data:
    - value_<id> → запрос в агента на value-разбор матча
    - deep_<id>  → запрос в агента на глубокий анализ
    - addbet_<id> → подсказка пользователю написать ставку
    """
    query = update.callback_query
    if not query:
        return

    await query.answer()

    data = query.data or ""
    user = update.effective_user
    user_id = user.id if user else 0

    # VALUE-разбор матча
    if data.startswith("value_"):
        match_id = data.split("_", 1)[1]
        try:
            text = await call_agent(user_id, f"value разбор матча {match_id}")
        except Exception as e:
            logger.exception("Ошибка при value-разборе матча %s: %s", match_id, e)
            text = "Не удалось получить value-разбор матча 😔\nПопробуй позже."
        await query.edit_message_text(text)

    # Глубокий анализ матча
    elif data.startswith("deep_"):
        match_id = data.split("_", 1)[1]
        try:
            text = await call_agent(user_id, f"глубокий анализ матча {match_id}")
        except Exception as e:
            logger.exception("Ошибка при глубоком разборе матча %s: %s", match_id, e)
            text = "Не удалось получить глубокий разбор матча 😔\nПопробуй позже."
        await query.edit_message_text(text)

    # Подсказка по добавлению ставки
    elif data.startswith("addbet_"):
        match_id = data.split("_", 1)[1]
        text = (
            f"💬 Напиши ставку на матч {match_id} в свободной форме.\n\n"
            "Примеры:\n"
            f"• ставка 2000 на матч {match_id} победа СКА по 1.85\n"
            f"• ставка 1500 на матч {match_id} тотал больше 5.5 за 1.90\n\n"
            "Я распознаю сумму, кэф, исход и событие и сохраню её в историю."
        )
        await query.edit_message_text(text)


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
    Для команд вида 'анализ матча 123' / 'анализ 123' —
    оборачиваем ответ в инлайн-клавиатуру с дополнительными действиями.
    """
    if not update.message:
        return

    user_id = update.effective_user.id
    text = update.message.text or ""
    norm = text.strip().lower()

    # Пытаемся понять, это запрос на анализ конкретного матча или нет
    match_id: str | None = None
    m = re.search(r"(анализ|разбор)\s+матча\s+(\d+)", norm)
    if not m:
        m = re.search(r"(анализ|разбор)\s+(\d+)", norm)
    if m:
        match_id = m.group(2)

    try:
        reply = await call_agent(user_id, text)
    except Exception as e:
        logger.exception("Ошибка при вызове агента: %s", e)
        reply = (
            "Не удалось связаться с сервером агента 😔\n"
            "Попробуй ещё раз чуть позже."
        )
        # даже при ошибке можно показать клавиатуру
        await update.message.reply_text(reply, reply_markup=build_main_keyboard())
        return

    # Если пользователь просит меню/помощь — показываем клавиатуру
    if norm in {"/start", "start", "меню", "help", "/help"}:
        await update.message.reply_text(reply, reply_markup=build_main_keyboard())
        return

    # Если это анализ матча с id — шлём ответ + инлайн-кнопки
    if match_id:
        await send_match_analysis(update, context, reply, match_id)
    else:
        # Обычный ответ без изменения клавиатуры
        await update.message.reply_text(reply)


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
    # Callback-кнопки под сообщениями
    app.add_handler(CallbackQueryHandler(callback_router))
    # Все текстовые сообщения (кроме команд) — в агент
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Блокирующий запуск polling в этом потоке,
    # без установки signal handlers (stop_signals=None)
    app.run_polling(stop_signals=None)
