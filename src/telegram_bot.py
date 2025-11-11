from __future__ import annotations

import os
import logging
from typing import Final

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

log = logging.getLogger("svc.bot")

# ==== Handlers ===============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Привет! 🤖 Бот запущен и работает.\n\n"
        "Команды:\n"
        "/health — проверка\n"
        "/bets — показать активные ставки\n"
        "/addbet <текст> — добавить ставку\n"
        "/clearbets <PIN> — очистить ставки\n"
    )
    await update.effective_chat.send_message(text)

async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_message("✅ Всё работает нормально!")

# Заглушки под БД — команды останутся рабочими, логика хранения может быть в db.py
async def cmd_bets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # тут потом подставишь выборку из БД
    await update.effective_chat.send_message("📊 Пока нет активных ставок.")

async def cmd_addbet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    note = " ".join(context.args) if context.args else ""
    if not note:
        await update.effective_chat.send_message("Использование: /addbet <текст>")
        return
    # тут потом сохранишь в БД
    await update.effective_chat.send_message(f"📝 Ставка добавлена: {note}")

async def cmd_clearbets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pin = " ".join(context.args) if context.args else ""
    # при необходимости сверяй PIN через env
    need_pin: Final[str] = os.getenv("ADMIN_PIN", "").strip()
    if need_pin and pin != need_pin:
        await update.effective_chat.send_message("❌ Неверный PIN.")
        return
    # тут очистишь БД
    await update.effective_chat.send_message("🧹 Ставки очищены.")

async def on_text_echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    txt = update.effective_message.text or ""
    log.info("[BOT] text: %s", txt)
    await update.effective_chat.send_message(f"Ты написал: {txt}")

# ==== Builder ================================================================

def build_bot_app() -> Application:
    """
    Создаёт и возвращает telegram.ext.Application.
    НЕ запускает polling — это делает service.py через initialize/start/updater.start_polling.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в переменных окружения.")

    app = (
        ApplicationBuilder()
        .token(token)
        .parse_mode(ParseMode.HTML)
        .concurrent_updates(True)  # можно выключить, если не нужно
        .build()
    )

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("bets", cmd_bets))
    app.add_handler(CommandHandler("addbet", cmd_addbet))
    app.add_handler(CommandHandler("clearbets", cmd_clearbets))

    # Эхо для обычного текста
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_echo))

    return app

