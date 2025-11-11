import asyncio
import os
from fastapi import FastAPI
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ===========================================
# Конфигурация
# ===========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

app = FastAPI(title="KHL Agent API")

_bot_app = None
_bot_task = None


# ===========================================
# Команды бота
# ===========================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! 🤖 Бот запущен и работает.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Доступные команды:\n/start\n/help\n/bets\n/refresh\n/health")


async def bets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Пока нет активных ставок.")


async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Обновление данных... (демо)")


async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Всё работает нормально!")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Эхо-ответ — показывает, что бот получает апдейты."""
    text = update.message.text
    print(f"[BOT] Получено сообщение: {text}")
    await update.message.reply_text(f"Ты написал: {text}")


# ===========================================
# Фоновая задача — запуск polling
# ===========================================

async def _run_bot_polling():
    """
    Фоновая задача: инициализируем приложение бота вручную и запускаем polling,
    не закрывая event loop uvicorn (иначе получаем ошибки "loop is running").
    """
    global _bot_app
    if not TELEGRAM_BOT_TOKEN:
        print("[BOT] TELEGRAM_BOT_TOKEN не задан — бот не будет запущен")
        return

    # Собираем приложение Telegram-бота
    _bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Регистрируем команды и хэндлеры
    _bot_app.add_handler(CommandHandler("start", start_cmd))
    _bot_app.add_handler(CommandHandler("help", help_cmd))
    _bot_app.add_handler(CommandHandler("bets", bets_cmd))
    _bot_app.add_handler(CommandHandler("refresh", refresh_cmd))
    _bot_app.add_handler(CommandHandler("health", health_cmd))
    _bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    print("[BOT] Инициализация…")
    await _bot_app.initialize()  # без run_polling, только подготовка

    print("[BOT] Старт…")
    await _bot_app.start()       # запускает сетевые подключения

    print("[BOT] Запускаю polling…")
    # ВАЖНО: запускаем только polling/updater в текущем event loop,
    # ничего не закрываем сами.
    await _bot_app.updater.start_polling()

    # Держим фоновую задачу пока polling не будет остановлен
    await _bot_app.updater.wait_until_closed()


# ===========================================
# События FastAPI
# ===========================================

@app.on_event("startup")
async def on_startup():
    """При запуске FastAPI создаём фоновую задачу с ботом."""
    global _bot_task
    print("[BOT] Фоновая задача создана")
    _bot_task = asyncio.create_task(_run_bot_polling())


@app.on_event("shutdown")
async def on_shutdown():
    """
    Корректная остановка бота при завершении FastAPI:
    останавливаем polling и приложение, НЕ закрывая event loop.
    """
    global _bot_app, _bot_task
    try:
        if _bot_app is not None:
            # Останавливаем polling
            try:
                await _bot_app.updater.stop()
            except Exception as e:
                print(f"[BOT] updater.stop() error: {e}")

            # Останавливаем приложение бота
            try:
                await _bot_app.stop()
            except Exception as e:
                print(f"[BOT] app.stop() error: {e}")

            # Финальная зачистка
            try:
                await _bot_app.shutdown()
            except Exception as e:
                print(f"[BOT] app.shutdown() error: {e}")

            _bot_app = None
    except Exception as e:
        print(f"[BOT] Ошибка при остановке: {e}")

    # Чистим фоновую задачу, если была
    if _bot_task is not None:
        _bot_task.cancel()
        try:
            await _bot_task
        except asyncio.CancelledError:
            pass
        _bot_task = None
        print("[BOT] Фоновая задача остановлена")


# ===========================================
# Проверочный эндпоинт
# ===========================================

@app.get("/healthz")
def healthz():
    return {"ok": True}



