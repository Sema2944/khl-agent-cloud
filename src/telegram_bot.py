# src/telegram_bot.pyfrom dotenv import load_dotenv
load_dotenv()

import os
import logging
from datetime import datetime
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    Defaults,
)

from sqlmodel import select

# Импорт из вашего модуля БД
# Ожидается, что в src/db.py есть:
#   - async_session (AsyncSession factory)
#   - Bet модель (id: int | None, text: str, created_at: datetime)
#   - Reminder (если понадобится дальше)
from .db import async_session, Bet  # Reminder не используем здесь

log = logging.getLogger("svc.bot")


# ======================
# Вспомогательные функции
# ======================

def _get_clear_pin() -> str:
    """
    PIN для команды /clearbets берём из переменной окружения CLEAR_PIN.
    Если нет — используем строку "100182" (как вы присылали ранее).
    """
    return os.getenv("CLEAR_PIN", "100182")


async def _add_bet_to_db(text: str) -> Bet:
    async with async_session() as session:
        bet = Bet(text=text, created_at=datetime.utcnow())  # type: ignore[arg-type]
        session.add(bet)
        await session.commit()
        await session.refresh(bet)
    return bet


async def _list_bets_from_db(limit: int = 50) -> list[Bet]:
    async with async_session() as session:
        result = await session.exec(
            select(Bet).order_by(Bet.id.desc()).limit(limit)
        )
        rows = list(result)
    return rows


async def _clear_bets_in_db() -> int:
    async with async_session() as session:
        # лучший способ — выполнить raw SQL удаление, чтобы быстро почистить
        # но тут сделаем безопасно через ORM: выбрать и удалить
        result = await session.exec(select(Bet))
        rows = list(result)
        count = len(rows)
        for r in rows:
            await session.delete(r)
        await session.commit()
    return count


# ======================
# Хэндлеры команд
# ======================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Привет! Я готов 😊\n\n"
        "<b>Команды:</b>\n"
        "/addbet &lt;текст&gt; — добавить ставку\n"
        "/bets — список последних ставок\n"
        f"/clearbets &lt;PIN&gt; — очистить (PIN: <code>{_get_clear_pin()}</code>)"
    )
    await update.message.reply_text(text)


async def cmd_addbet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    payload = (update.message.text or "").split(maxsplit=1)
    if len(payload) < 2:
        await update.message.reply_text(
            "❗ Использование: <code>/addbet Текст ставки</code>"
        )
        return

    bet_text = payload[1].strip()
    if not bet_text:
        await update.message.reply_text("❗ Текст пустой.")
        return

    try:
        bet = await _add_bet_to_db(bet_text)
        await update.message.reply_text(f"✅ Ставка добавлена: #{bet.id}")
    except Exception as e:
        log.exception("Ошибка добавления ставки: %s", e)
        await update.message.reply_text("❌ Ошибка при добавлении ставки.")


async def cmd_bets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    try:
        rows = await _list_bets_from_db(limit=50)
        if not rows:
            await update.message.reply_text("Ставок пока нет.")
            return

        lines = []
        for b in rows[::-1]:  # старые сверху
            created = getattr(b, "created_at", None)
            created_str = (
                created.strftime("%Y-%m-%d %H:%M") if isinstance(created, datetime) else "-"
            )
            lines.append(f"#{b.id} — {b.text} (at {created_str})")
        answer = "<b>Ставки:</b>\n" + "\n".join(lines)
        await update.message.reply_text(answer)
    except Exception as e:
        log.exception("Ошибка получения списка ставок: %s", e)
        await update.message.reply_text("❌ Ошибка получения списка.")


async def cmd_clearbets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    parts = (update.message.text or "").split(maxsplit=1)
    pin = parts[1].strip() if len(parts) > 1 else ""
    if pin != _get_clear_pin():
        await update.message.reply_text("❌ Неверный PIN.")
        return

    try:
        count = await _clear_bets_in_db()
        await update.message.reply_text(f"🧹 Удалено ставок: {count}")
    except Exception as e:
        log.exception("Ошибка очистки ставок: %s", e)
        await update.message.reply_text("❌ Ошибка при очистке.")


# ======================
# Сборка приложения бота
# ======================

async def build_bot_app() -> Application:
    """
    Создаёт и возвращает PTB Application.
    Используется из FastAPI (в service.py) в on_startup.
    """
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("Не задан TELEGRAM_TOKEN в переменных окружения.")

    app = (
        ApplicationBuilder()
        .token(token)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .build()
    )

    # Регистрация хэндлеров
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("addbet", cmd_addbet))
    app.add_handler(CommandHandler("bets", cmd_bets))
    app.add_handler(CommandHandler("clearbets", cmd_clearbets))

    # Периодический парсинг линии — включится, если установлен extra "job-queue"
    # в pyproject.toml: python-telegram-bot = { version = "==21.6", extras = ["job-queue"] }
    if app.job_queue is not None:
        # Заглушка задачи: поставь сюда свой парсер линии
        async def _job_refresh_line(ctx: ContextTypes.DEFAULT_TYPE) -> None:
            # TODO: тут вызывай свой парсер линии/запись в БД
            pass

        app.job_queue.run_repeating(_job_refresh_line, interval=300, first=5)
    else:
        log.info("JobQueue недоступен (не установлен extra 'job-queue'). Автопарсинг выключен.")

    return app





