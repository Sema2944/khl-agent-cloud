# src/service.py
from __future__ import annotations

import asyncio
import os
import pathlib
import sqlite3
from contextlib import contextmanager
from typing import Optional, List, Tuple

from fastapi import FastAPI
from pydantic import BaseModel

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==========
# Конфиг
# ==========
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_PIN = os.getenv("ADMIN_PIN", "0000").strip()

DATA_DIR = pathlib.Path("./data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bets.db"

# ==========
# БД (SQLite)
# ==========
def _init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                pick TEXT NOT NULL,
                odds REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()

@contextmanager
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()

def add_bet(event: str, pick: str, odds: float) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO bets (event, pick, odds) VALUES (?, ?, ?)",
            (event, pick, odds),
        )
        conn.commit()
        return cur.lastrowid

def list_bets() -> List[Tuple[int, str, str, float, str]]:
    with db() as conn:
        cur = conn.execute(
            "SELECT id, event, pick, odds, created_at FROM bets ORDER BY id DESC"
        )
        return list(cur.fetchall())

def clear_bets() -> int:
    with db() as conn:
        cur = conn.execute("DELETE FROM bets")
        conn.commit()
        return cur.rowcount

# ==========
# Telegram bot
# ==========
_bot_app: Optional[Application] = None
_bot_task: Optional[asyncio.Task] = None

async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! 🤖 Бот запущен и работает.")
    await update.message.reply_text("Доступные команды:\n"
                                    "/addbet <событие>; <ставка>; <коэфф>\n"
                                    "/bets — показать все ставки\n"
                                    f"/clearbets <pin> — очистить (pin по умолчанию {ADMIN_PIN})")

async def _cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("✅ Всё работает нормально!")

async def _cmd_echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await update.message.reply_text(f"Ты написал: {update.message.text}")

def _parse_addbet_args(text: str) -> Tuple[str, str, float]:
    """
    Ожидаемый формат: /addbet Событие; П1; 1.85
    Возвращает (event, pick, odds)
    """
    # отрезаем "/addbet"
    parts = text.split(" ", 1)
    if len(parts) < 2:
        raise ValueError("После /addbet укажи параметры как: Событие; Ставка; Коэфф")

    payload = parts[1]
    chunks = [c.strip() for c in payload.split(";")]
    if len(chunks) != 3:
        raise ValueError("Нужно три параметра через ';': Событие; Ставка; Коэфф")

    event, pick, odds_str = chunks
    try:
        odds = float(odds_str.replace(",", "."))
    except ValueError:
        raise ValueError("Коэффициент должен быть числом, например: 1.85")

    if not event or not pick:
        raise ValueError("Событие и ставка не должны быть пустыми")

    return event, pick, odds

async def _cmd_addbet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.message.text:
            await update.message.reply_text("Не нашёл текста. Пример: /addbet SKA–CSKA; П1; 1.85")
            return
        event, pick, odds = _parse_addbet_args(update.message.text)
        bet_id = add_bet(event, pick, odds)
        await update.message.reply_text(f"✅ Ставка добавлена (#{bet_id}): {event} — {pick} @ {odds}")
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")

async def _cmd_bets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = list_bets()
    if not rows:
        await update.message.reply_text("📊 Пока нет активных ставок.")
        return
    # Форматируем список
    lines = []
    for bet_id, event, pick, odds, created_at in rows:
        lines.append(f"#{bet_id} • {event} — {pick} @ {odds} • {created_at}")
    text = "📊 Активные ставки:\n" + "\n".join(lines)
    # Telegram ограничивает длину сообщения, но наш список небольшой; при необходимости можно пагинировать.
    await update.message.reply_text(text)

async def _cmd_clearbets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Команда: /clearbets <pin>
    pin = ""
    if update.message and update.message.text:
        parts = update.message.text.strip().split(" ", 1)
        if len(parts) == 2:
            pin = parts[1].strip()

    if pin != ADMIN_PIN:
        await update.message.reply_text("⛔ Неверный PIN. Очистка запрещена.")
        return

    n = clear_bets()
    await update.message.reply_text(f"🧹 Удалено записей: {n}")

async def _msg_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # простой эхо на всё остальное
    if update.message and update.message.text:
        await update.message.reply_text(f"Ты написал: {update.message.text}")

def _build_bot() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("Переменная окружения TELEGRAM_BOT_TOKEN не задана")
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("health", _cmd_health))
    app.add_handler(CommandHandler("echo", _cmd_echo))

    app.add_handler(CommandHandler("addbet", _cmd_addbet))
    app.add_handler(CommandHandler("bets", _cmd_bets))
    app.add_handler(CommandHandler("clearbets", _cmd_clearbets))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _msg_text))
    return app

async def _run_bot_polling() -> None:
    """
    Запуск polling внутри уже работающего event loop FastAPI/Uvicorn.
    ВАЖНО: запрещаем закрытие цикла и обработку сигналов изнутри PTB.
    """
    global _bot_app
    _bot_app = _build_bot()
    print("[BOT] Фоновая задача создана")
    # Параметры, совместимые с uvicorn/uvloop
    await _bot_app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        stop_signals=None,     # не перехватывать SIGINT/SIGTERM
        close_loop=False,      # не закрывать event loop, он общий с uvicorn
        poll_interval=1.0,     # по умолчанию 0.0; поставим 1.0 чтобы уйти от tight loop
    )

# ==========
# FastAPI app
# ==========
app = FastAPI(title="KHL Agent API")

class Health(BaseModel):
    ok: bool

@app.get("/healthz", response_model=Health)
async def healthz() -> Health:
    return Health(ok=True)

@app.on_event("startup")
async def on_startup() -> None:
    _init_db()
    # Запускаем бота в фоне
    global _bot_task
    loop = asyncio.get_event_loop()
    _bot_task = loop.create_task(_run_bot_polling())
    print("[BOT] Запускаю polling…")

@app.on_event("shutdown")
async def on_shutdown() -> None:
    # Аккуратно останавливаем бота (если он был создан)
    global _bot_app, _bot_task
    if _bot_app is not None:
        try:
            await _bot_app.stop()
        except Exception as e:
            print(f"[BOT] Ошибка при остановке: {e}")
    if _bot_task is not None:
        try:
            await _bot_task
        except Exception:
            # Задача уже могла завершиться
            pass


