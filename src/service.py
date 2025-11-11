# === BOT LIFECYCLE (без run_polling) ===
from telegram import Update
from .telegram_bot import build_bot_app  # как у тебя раньше

import asyncio
import logging

log = logging.getLogger("svc")

_bot_app = None        # type: ignore
_bot_task: asyncio.Task | None = None

async def _bot_start() -> None:
    """Инициализация и старт polling без блокировки event loop."""
    global _bot_app
    if _bot_app is None:
        _bot_app = build_bot_app()
    try:
        # ВАЖНО: три шага вместо run_polling
        await _bot_app.initialize()
        await _bot_app.start()
        await _bot_app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        log.info("[BOT] Polling started")
    except Exception as e:
        log.exception("[BOT] Ошибка при запуске polling: %s", e)

async def _bot_stop() -> None:
    """Корректная остановка polling и приложения бота."""
    if _bot_app is None:
        return
    try:
        # Останавливаем polling, затем само приложение
        await _bot_app.updater.stop()
    except Exception as e:
        log.warning("[BOT] updater.stop() error: %s", e)
    try:
        await _bot_app.stop()
        await _bot_app.shutdown()
    except Exception as e:
        log.warning("[BOT] stop/shutdown error: %s", e)
    log.info("[BOT] Stopped")


# === ВКЛЮЧЕНИЕ В СТАРТ/ШАТДАУН FASTAPI ===
from fastapi import FastAPI

app = FastAPI()

# если у тебя есть init_db() — вызывай здесь же
from .db import init_db
@app.on_event("startup")
async def on_startup():
    log.info("[APP] Запуск приложения...")
    await init_db()

    # стартуем LINE-воркер, если он у тебя есть
    # (оставь как было; ниже пример, если уже реализовал)
    global _line_task
    try:
        _line_task  # просто обращаемся, чтобы mypy не ругался
    except NameError:
        _line_task = None  # если нет — игнорируй

    if _line_task is None:
        try:
            _line_task = asyncio.create_task(_upsert_line())  # если функция есть в файле
            log.info("[LINE] Background task started")
        except NameError:
            pass  # если нет линии — ничего

    # стартуем бота неблокирующим таском
    global _bot_task
    if _bot_task is None:
        _bot_task = asyncio.create_task(_bot_start())
        log.info("[BOT] Запускаю polling…")

@app.on_event("shutdown")
async def on_shutdown():
    log.info("[APP] Остановка приложения...")

    # останавливаем бота
    await _bot_stop()
    global _bot_task
    if _bot_task:
        try:
            await _bot_task
        except Exception:
            pass
        _bot_task = None

    # останавливаем LINE-воркер (если он есть)
    global _line_task
    try:
        if _line_task:
            _line_task.cancel()
            try:
                await _line_task
            except asyncio.CancelledError:
                pass
            _line_task = None
    except NameError:
        pass  # линии нет — ок


# === Простейший health endpoint (оставь твой, если уже есть) ===
@app.get("/")
async def root():
    return {"ok": True}


# ========================
# Telegram-команды
# ========================

async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Привет! 🤖 Бот запущен и работает.\n\n"
        "Команды:\n"
        "/health — проверка\n"
        "/bets — показать активные ставки\n"
        "/addbet <текст> — добавить ставку\n"
        "/clearbets <PIN> — очистить ставки\n"
        "/remind <текст> через Nмин|Nчас|Nд — напоминание\n"
    )
    await update.message.reply_text(text)


async def _cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Всё работает нормально!")


async def _cmd_addbet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text("Использование: /addbet <текст ставки>")
        return
    user = update.effective_user
    async with async_session() as s:
        bet = Bet(user_id=user.id, username=user.username, text=text)
        s.add(bet)
        await s.commit()
        await s.refresh(bet)
    await update.message.reply_text(f"✅ Ставка сохранена (#{bet.id})")


async def _cmd_bets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as s:
        res = await s.exec(select(Bet).order_by(Bet.id.desc()).limit(10))
        rows = list(res)
    if not rows:
        await update.message.reply_text("📊 Пока нет активных ставок.")
        return
    lines = [f"#{b.id} [{b.username or b.user_id}] {b.text}" for b in rows]
    await update.message.reply_text("Последние ставки:\n" + "\n".join(lines))


async def _cmd_clearbets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pin = context.args[0] if context.args else ""
    if pin != ADMIN_PIN:
        await update.message.reply_text("❌ Неверный PIN.")
        return
    async with async_session() as s:
        await s.exec("DELETE FROM bet")
        await s.commit()
    await update.message.reply_text("🗑️ Все ставки очищены.")


# ========================
# Напоминания
# ========================

def _parse_delay(args: list[str]) -> timedelta | None:
    joined = " ".join(args).lower()
    if "через" not in joined:
        return None
    after = joined.split("через", 1)[1].strip()
    num = ""
    unit = ""
    for ch in after:
        if ch.isdigit():
            num += ch
        elif ch.isalpha():
            unit += ch
        elif ch.isspace():
            continue
        else:
            break
    if not num:
        return None
    n = int(num)
    if unit.startswith("мин"):
        return timedelta(minutes=n)
    if unit.startswith("час"):
        return timedelta(hours=n)
    if unit.startswith("д"):
        return timedelta(days=n)
    return None


async def _cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        await update.message.reply_text("Использование: /remind <текст> через <Nмин|Nчас|Nд>")
        return
    delay = _parse_delay(args)
    if not delay:
        await update.message.reply_text("Не понял время. Пример: /remind Позвонить через 15мин")
        return
    text = " ".join(args).split("через")[0].strip()
    if not text:
        await update.message.reply_text("Укажи текст напоминания до слова 'через'.")
        return
    run_at = datetime.utcnow() + delay
    async with async_session() as s:
        r = Reminder(
            user_id=update.effective_user.id,
            chat_id=update.effective_chat.id,
            text=text,
            run_at=run_at,
        )
        s.add(r)
        await s.commit()
        await s.refresh(r)
    await update.message.reply_text(f"⏰ Напоминание #{r.id} запланировано.")


async def _reminder_worker():
    try:
        while True:
            now = datetime.utcnow()
            async with async_session() as s:
                res = await s.exec(
                    select(Reminder).where(Reminder.done == False, Reminder.run_at <= now)
                )
                due = list(res)
                for r in due:
                    try:
                        await _bot_app.bot.send_message(chat_id=r.chat_id, text=f"🔔 Напоминание: {r.text}")
                        r.done = True
                        s.add(r)
                        await s.commit()
                    except Exception as e:
                        log.error("[REM] Ошибка отправки: %s", e)
            await asyncio.sleep(10)
    except asyncio.CancelledError:
        log.info("[REM] Воркер остановлен.")


# ========================
# Telegram Bot запуск
# ========================

async def _run_bot_polling():
    global _bot_app
    log.info("[BOT] Запускаю polling…")

    _bot_app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    _bot_app.add_handler(CommandHandler("start", _cmd_start))
    _bot_app.add_handler(CommandHandler("health", _cmd_health))
    _bot_app.add_handler(CommandHandler("addbet", _cmd_addbet))
    _bot_app.add_handler(CommandHandler("bets", _cmd_bets))
    _bot_app.add_handler(CommandHandler("clearbets", _cmd_clearbets))
    _bot_app.add_handler(CommandHandler("remind", _cmd_remind))
    _bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _cmd_start))

    try:
        await _bot_app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            close_loop=False,
            drop_pending_updates=True,
        )
    except Exception as e:
        log.error("[BOT] Ошибка в run_polling: %s", e)


# ========================
# FastAPI endpoints
# ========================

@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.on_event("startup")
async def on_startup():
    log.info("[APP] Запуск приложения...")
    await init_db()
    global _bot_task, _reminder_task
    if _bot_task is None or _bot_task.done():
        _bot_task = asyncio.create_task(_run_bot_polling())
    if _reminder_task is None or _reminder_task.done():
        _reminder_task = asyncio.create_task(_reminder_worker())


@app.on_event("shutdown")
async def on_shutdown():
    log.info("[APP] Остановка приложения...")
    global _bot_task, _reminder_task
    try:
        if _bot_task:
            _bot_task.cancel()
    except Exception as e:
        log.warning("[BOT] Ошибка при остановке: %s", e)
    try:
        if _reminder_task:
            _reminder_task.cancel()
    except Exception as e:
        log.warning("[REM] Ошибка при остановке: %s", e)



