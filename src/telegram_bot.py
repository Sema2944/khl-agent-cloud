import os
import logging
import asyncio

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
        # даже при ошибке можно показать клавиатуру
        await update.message.reply_text(reply, reply_markup=build_main_keyboard())
        return

    # Если пользователь просит меню/помощь — показываем клавиатуру
    if norm in {"/start", "start", "меню", "help", "/help"}:
        await update.message.reply_text(reply, reply_markup=build_main_keyboard())
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
    # Все текстовые сообщения (кроме команд) — в агент
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Блокирующий запуск polling в этом потоке,
    # без установки signal handlers (stop_signals=None)
    app.run_polling(stop_signals=None)
