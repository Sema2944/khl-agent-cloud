import os
import logging
from html import escape as h

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    Defaults,
)

log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    greet = (
        f"Привет{', ' + h(user.first_name) if user and user.first_name else ''}! ✌️\n\n"
        "Я бот для ставок. Вот что я умею:"
    )
    help_text = (
        "Команды:\n"
        "• /health — проверка\n"
        "• /bets — показать активные ставки\n"
        "• /addbet <текст> — добавить ставку\n"
        "• /clearbets <PIN> — очистить все ставки"
    )
    await update.effective_chat.send_message(greet)
    await update.effective_chat.send_message(help_text)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "Команды:\n"
        "• /health — проверка\n"
        "• /bets — показать активные ставки\n"
        "• /addbet <текст> — добавить ставку\n"
        "• /clearbets <PIN> — очистить все ставки"
    )
    await update.effective_chat.send_message(help_text)


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_message("✅ OK")


def _require_token() -> str:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Не задан TELEGRAM_TOKEN в переменных окружения.")
    return TELEGRAM_TOKEN


async def build_bot_app():
    """Создаёт Application, удаляет вебхук (если был), регистрирует хендлеры."""
    token = _require_token()

    app = (
        ApplicationBuilder()
        .token(token)
        .defaults(Defaults(parse_mode=ParseMode.HTML))  # PTB v21 корректный способ
        .build()
    )

    # Сбросить вебхук (если когда-то включали), чтобы работал long polling
    await app.bot.delete_webhook(drop_pending_updates=True)
    log.info("✅ Webhook удалён (drop_pending_updates=True). Переходим на long polling.")

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("health", cmd_health))

    return app







