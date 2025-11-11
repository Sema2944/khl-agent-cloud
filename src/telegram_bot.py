# src/telegram_bot.py
from __future__ import annotations

import os
import logging
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    Defaults,
    filters,
)

from .db import async_session, Bet, Reminder  # модели БД, если нужны в хендлерах
from sqlmodel import select

log = logging.getLogger("svc.bot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 🤖 Бот запущен и работает.\n\n"
        "Команды:\n"
        "/health — проверка\n"
        "/bets — показать активные ставки\n"
        "/addbet <текст> — добавить ставку\n"
        "/clearbets <PIN> — очистить"
    )


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Всё работает нормально!")


async def cmd_bets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        res = await session.exec(select(Bet).order_by(Bet.created_at.desc()).limit(20))
        items = res.all()
    if not items:
        await update.message.reply_text("📊 Пока нет активных ставок.")
        return
    lines = [f"• #{b.id}: {b.text}" for b in items]
    await update.message.reply_text("📊 Активные ставки:\n" + "\n".join(lines))


async def cmd_addbet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Использование: /addbet <текст>")
        return
    bet = Bet(text=text)
    async with async_session() as session:
        session.add(bet)
        await session.commit()
        await session.refresh(bet)
    await update.message.reply_text(f"✅ Ставка добавлена: #{bet.id}")


async def cmd_clearbets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pin = (context.args[0].strip() if context.args else "")
    admin_pin = os.getenv("ADMIN_PIN", "")
    if not admin_pin or pin != admin_pin:
        await update.message.reply_text("❌ Неверный PIN.")
        return
    async with async_session() as session:
        # мягкая очистка таблицы ставок
        res = await session.exec(select(Bet))
        for b in res:
            await session.delete(b)
        await session.commit()
    await update.message.reply_text("🧹 Готово, ставки очищены.")


async def echo_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # простая заглушка на любые тексты
    await update.message.reply_text("Команда не распознана. Напишите /start.")


async def build_bot_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в переменных окружения")

    # ВАЖНО: parse_mode через Defaults
    defaults = Defaults(parse_mode=ParseMode.HTML)

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .defaults(defaults)
        .build()
    )

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("bets", cmd_bets))
    app.add_handler(CommandHandler("addbet", cmd_addbet))
    app.add_handler(CommandHandler("clearbets", cmd_clearbets))

    # Фоллбек на текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_text))

    return app


