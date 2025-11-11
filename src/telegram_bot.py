from __future__ import annotations

import os
import asyncio
import datetime as dt
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    Defaults,
)

from .db import Bet, Reminder, async_session, search_events
from .parsing import refresh_line

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_PIN = os.getenv("ADMIN_PIN", "100182")  # твой PIN

async def build_bot_app():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .build()
    )

    app.add_handler(CommandHandler("health", _cmd_health))
    app.add_handler(CommandHandler("addbet", _cmd_addbet))
    app.add_handler(CommandHandler("bets", _cmd_bets))
    app.add_handler(CommandHandler("clearbets", _cmd_clearbets))
    app.add_handler(CommandHandler("line", _cmd_line))

    # периодическое обновление линии
    app.job_queue.run_repeating(_job_refresh_line, interval=300, first=5)

    return app

# === Commands ===

async def _cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("OK")

async def _cmd_addbet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Варианты:
    /addbet просто текст...
    /addbet <EVENT_ID> произвольный текст...
    """
    if not update.message:
        return
    text = update.message.text or ""
    args = text.split(maxsplit=1)
    payload = args[1] if len(args) > 1 else ""

    # если первый токен выглядит как ID события (число) — отделим
    event_id: Optional[int] = None
    rest = payload
    if payload:
        first = payload.split()[0]
        if first.isdigit():
            try:
                event_id = int(first)
                rest = payload[len(first):].strip()
            except Exception:
                event_id = None

    bet_text = rest or payload or "Ставка"
    async with async_session() as s:
        b = Bet(user_id=update.effective_user.id if update.effective_user else None, text=bet_text)
        s.add(b)
        await s.commit()
        await s.refresh(b)

    await update.message.reply_text(f"✅ Ставка добавлена: #{b.id}")

async def _cmd_bets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    async with async_session() as s:
        from sqlmodel import select
        res = await s.exec(select(Bet).order_by(Bet.id.desc()).limit(20))
        rows = res.all()
    if not rows:
        await update.message.reply_text("Пусто.")
        return
    lines = [f"<b>#{b.id}</b> — {b.text} <i>({b.created_at:%Y-%m-%d %H:%M})</i>" for b in rows]
    await update.message.reply_text("\n".join(lines))

async def _cmd_clearbets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    pin = parts[1] if len(parts) > 1 else ""
    if pin != ADMIN_PIN:
        await update.message.reply_text("❌ Неверный PIN.")
        return
    async with async_session() as s:
        from sqlmodel import delete
        await s.exec(delete(Reminder))
        await s.exec(delete(Bet))
        await s.commit()
    await update.message.reply_text("✅ Очищено.")

async def _cmd_line(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /line [поиск] """
    if not update.message:
        return
    q = None
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        q = parts[1]
    rows = await search_events(q, limit=10)
    if not rows:
        await update.message.reply_text("Линия пуста. Обновляю…")
        cnt = await refresh_line()
        await update.message.reply_text(f"Подтянул: {cnt} событий.")
        rows = await search_events(q, limit=10)
        if not rows:
            await update.message.reply_text("Пока нет данных.")
            return
    lines = []
    for ev in rows:
        when = ev.starts_at.strftime("%Y-%m-%d %H:%M") if ev.starts_at else "—"
        odds = " / ".join(
            x for x in [
                f"{ev.odds1:.2f}" if ev.odds1 else None,
                f"{ev.oddsX:.2f}" if ev.oddsX else None,
                f"{ev.odds2:.2f}" if ev.odds2 else None,
            ] if x
        )
        lines.append(
            f"<b>ID {ev.id}</b> | {ev.league or ev.sport or '—'} | {when}\n"
            f"{ev.team1 or '—'} — {ev.team2 or '—'}\n"
            f"Кэфы: {odds or '—'}\n"
            f"/addbet {ev.id} моя заметка по ставке"
        )
    await update.message.reply_text("\n\n".join(lines))

# === Jobs ===

async def _job_refresh_line(context: ContextTypes.DEFAULT_TYPE):
    try:
        cnt = await refresh_line()
        # можно логгировать в console/service logs
    except Exception:
        pass



