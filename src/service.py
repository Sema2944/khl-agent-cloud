# src/service.py
from __future__ import annotations

import os
import asyncio
import logging
from contextlib import suppress
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from telegram import Update
from telegram.ext import ContextTypes

# твои модули
from .telegram_bot import build_bot_app
from .db import init_db, async_session, Bet, Reminder  # если Bet/Reminder понадобятся в ручках

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("svc")

app = FastAPI(title="KHL Agent API")

# Глобальные ссылки на приложение бота и фоновую таску polling
_bot_app = None
_bot_task: Optional[asyncio.Task] = None


@app.get("/", response_class=PlainTextResponse)
async def root():
    return "OK"


@app.get("/health", response_class=PlainTextResponse)
async def health():
    return "healthy"


# ==== Примеры команд-хендлеров, которые регистрируются в telegram_bot.build_bot_app ====
# Оставлены тут, если тебе нужно быстро подсмотреть сигнатуры
async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 🤖 Бот запущен и работает.\n\n"
        "Команды:\n"
        "/health — проверка\n"
        "/bets — показать активные ставки\n"
        "/addbet <текст> — добавить ставку\n"
        "/clearbets <PIN> — очистить"
    )


async def _cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Всё работает нормально!")


async def _cmd_bets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # пример чтения из БД
    from sqlmodel import select
    async with async_session() as session:
        res = await session.exec(select(Bet).order_by(Bet.created_at.desc()))
        items = res.all()
    if not items:
        await update.message.reply_text("📊 Пока нет активных ставок.")
        return
    lines = [f"• #{b.id}: {b.text}" for b in items[:20]]
    await update.message.reply_text("📊 Активные ставки:\n" + "\n".join(lines))


# ========================= Жизненный цикл FastAPI =========================

@app.on_event("startup")
async def on_startup():
    global _bot_app, _bot_task

    log.info("[APP] Запуск приложения...")
    # 1) Инициализируем БД (создаст таблицы, если их нет)
    await init_db()

    # 2) Строим приложение Telegram-бота
    #    Внутри build_bot_app ты регистрируешь хендлеры (команды, сообщения и т.д.)
    _bot_app = await build_bot_app()

    # 3) Запускаем polling НЕ блокирующим способом
    #    run_polling вызывать нельзя (он пытается рулить своим loop), поэтому вручную:
    async def _polling():
        try:
            log.info("[BOT] Инициализация...")
            await _bot_app.initialize()
            await _bot_app.start()
            # Важно: использовать updater.start_polling внутри уже запущенного event loop.
            await _bot_app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                poll_interval=1.0,
                bootstrap_retries=0,
                drop_pending_updates=True,
            )
            log.info("[BOT] Polling запущен")
            # Ждём, пока нас не отменят при shutdown
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # Нормальная остановка
            log.info("[BOT] Остановка (cancelled)")
            raise
        except Exception as e:
            log.error("[BOT] Ошибка в polling: %s", e, exc_info=True)
        finally:
            # Аккуратная остановка апдейтера/приложения
            with suppress(Exception):
                if _bot_app.updater:
                    await _bot_app.updater.stop()
            with suppress(Exception):
                await _bot_app.stop()
            with suppress(Exception):
                await _bot_app.shutdown()
            log.info("[BOT] Остановлен")

    log.info("[BOT] Запускаю polling…")
    _bot_task = asyncio.create_task(_polling(), name="tg-polling")


@app.on_event("shutdown")
async def on_shutdown():
    global _bot_task
    log.info("[APP] Остановка приложения...")
    # Останавливаем фонового бота
    if _bot_task and not _bot_task.done():
        _bot_task.cancel()
        with suppress(asyncio.CancelledError):
            await _bot_task
    log.info("[APP] Остановлено.")


# ========================= Примеры HTTP-ручек к БД (по желанию) =========================

from pydantic import BaseModel


class AddBetBody(BaseModel):
    text: str


@app.post("/api/bets")
async def add_bet(body: AddBetBody):
    """Добавить ставку через HTTP (опционально, для интеграций)."""
    from sqlmodel import select
    bet = Bet(text=body.text)
    async with async_session() as session:
        session.add(bet)
        await session.commit()
        await session.refresh(bet)
        # вернём последние 10 ставок
        res = await session.exec(select(Bet).order_by(Bet.created_at.desc()).limit(10))
        latest = res.all()
    return {"ok": True, "created": {"id": bet.id, "text": bet.text}, "latest": [
        {"id": b.id, "text": b.text} for b in latest
    ]}


@app.get("/api/bets")
async def list_bets():
    from sqlmodel import select
    async with async_session() as session:
        res = await session.exec(select(Bet).order_by(Bet.created_at.desc()).limit(50))
        items = res.all()
    return [{"id": b.id, "text": b.text} for b in items]



