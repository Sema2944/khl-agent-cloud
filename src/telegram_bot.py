from datetime import timezone
from src import khl_client
import logging
import os
import inspect
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

# Глобальный объект приложения бота
_bot_app: Optional[Application] = None


# ====================== HANDLERS ======================

async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    # ВАЖНО: только обычный текст, без <b>, <i>, <текст> и т.п.
    text = (
        "Привет! Я бот учёта ставок.\n\n"
        "Доступные команды:\n"
        "/addbet Описание ставки — добавить ставку\n"
        "/clearbets — очистить список ставок\n"
        "/bets — показать актуальные линии и рекомендации\n"
        "/help — показать справку\n"
    )

    await update.message.reply_text(text)


async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    text = (
        "Справка по боту:\n\n"
        "/start — запустить бота и показать приветствие\n"
        "/addbet Описание ставки — добавить ставку\n"
        "/clearbets — удалить все сохранённые ставки\n"
        "/bets — показать актуальные линии и рекомендации\n"
    )

    await update.message.reply_text(text)


async def _cmd_addbet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    if not context.args:
        await update.message.reply_text("Использование: /addbet Описание ставки")
        return

    description = " ".join(context.args)
    await update.message.reply_text(f"Ставка добавлена: {description}")


async def _cmd_clearbets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    # Здесь позже можно будет вызвать очистку из БД
    await update.message.reply_text("Все ставки очищены (заглушка).")


async def _cmd_bets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Показывает пользователю актуальные линии и простые рекомендации.

    Здесь сделана аккуратная интеграция с khl_client:
    - если в khl_client есть функция get_latest_lines (sync или async) — пытаемся её вызвать;
    - если нет или она падает — показываем понятный текст-заглушку, но НЕ роняем бота.
    """
    if update.message is None:
        return

    bets_text: str | None = None

    # Пытаемся мягко использовать khl_client, не ломая бота
    if hasattr(khl_client, "get_latest_lines"):
        try:
            raw_result = khl_client.get_latest_lines()  # может быть sync или async

            if inspect.iscoroutine(raw_result):
                raw_result = await raw_result

            # Дальше пытаемся красиво отформатировать разные варианты ответа

            # 1) Если вернули строку — отправляем как есть
            if isinstance(raw_result, str):
                bets_text = raw_result

            # 2) Если вернули список — предполагаем, что там словари или объекты с атрибутами
            elif isinstance(raw_result, list) and raw_result:
                lines_lines: list[str] = ["Актуальные линии:\n"]
                for item in raw_result[:10]:  # не больше 10, чтобы не спамить
                    try:
                        # Пытаемся вытащить поля по «типовым» именам
                        league = getattr(item, "league", None) or getattr(item, "champ", None) or ""
                        teams = getattr(item, "teams", None) or getattr(item, "match", None) or ""
                        coef = getattr(item, "coef", None) or getattr(item, "odds", None) or ""
                        reco = getattr(item, "recommendation", None) or ""

                        # Если это dict
                        if isinstance(item, dict):
                            league = item.get("league") or item.get("champ") or league
                            teams = item.get("teams") or item.get("match") or teams
                            coef = item.get("coef") or item.get("odds") or coef
                            reco = item.get("recommendation") or item.get("reco") or reco

                        line_parts = []
                        if league:
                            line_parts.append(f"[{league}]")
                        if teams:
                            line_parts.append(str(teams))
                        if coef:
                            line_parts.append(f"кэф: {coef}")
                        if reco:
                            line_parts.append(f"рекомендуем: {reco}")

                        if line_parts:
                            lines_lines.append(" • " + " | ".join(map(str, line_parts)))
                    except Exception as e:
                        logger.exception("Ошибка форматирования линии: %s", e)
                        continue

                if len(lines_lines) > 1:
                    bets_text = "\n".join(lines_lines)

            # 3) На всякий случай — просто превращаем в строку
            if bets_text is None and raw_result is not None:
                bets_text = f"Актуальные данные по линиям:\n{raw_result}"

        except Exception as e:
            logger.exception("Ошибка при обращении к khl_client.get_latest_lines: %s", e)

    # Если khl_client ничего не дал или его функции нет — выводим заглушку
    if not bets_text:
        bets_text = (
            "Пока я показываю только заглушку по линиям.\n\n"
            "В ближайшее время сюда можно будет подтянуть реальные линии и рекомендации "
            "через khl_client (функция get_latest_lines или аналогичная).\n\n"
            "Сейчас доступно:\n"
            "• /addbet Описание ставки — вручную добавить свою ставку\n"
            "• /clearbets — очистить список ставок\n"
            "• /help — список всех команд\n"
        )

    await update.message.reply_text(bets_text)


async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "Я пока понимаю только команды.\n"
        "Напиши /help, чтобы посмотреть список доступных команд."
    )


# ====================== ВСПОМОГАТЕЛЬНОЕ ======================

def _create_application(token: str) -> Application:
    """
    Создаём Application и регистрируем хендлеры.
    НИКАКОГО parse_mode здесь нет.
    """
    app = ApplicationBuilder().token(token).build()

    # Команды
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("help", _cmd_help))
    app.add_handler(CommandHandler("addbet", _cmd_addbet))
    app.add_handler(CommandHandler("clearbets", _cmd_clearbets))
    app.add_handler(CommandHandler("bets", _cmd_bets))

    # Любой текст без команды
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))

    return app


async def build_bot_app() -> Optional[Application]:
    """
    Создаёт и кэширует Application, если задан TELEGRAM_TOKEN.
    Ничего не запускает, только строит.
    """
    global _bot_app

    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.warning("⚠️ TELEGRAM_TOKEN не задан — бот не будет запущен.")
        return None

    if _bot_app is None:
        _bot_app = _create_application(token)
        logger.info("[BOT] Application создан.")

    return _bot_app


# ====================== ЗАПУСК / ОСТАНОВКА (ASGI-style) ======================

_bot_started: bool = False  # наш флаг, чтобы не стартовать несколько раз


async def start_bot_polling() -> None:
    """
    Стартуем бота внутри того же event loop, что и FastAPI/uvicorn.
    Без run_polling, без потоков — только initialize/start/updater.start_polling.
    """
    global _bot_app, _bot_started

    if _bot_app is None:
        logger.warning("[BOT] start_bot_polling вызван, но _bot_app is None.")
        return

    if _bot_started:
        logger.info("[BOT] Уже запущен, пропускаем повторный start.")
        return

    # Инициализация Application
    await _bot_app.initialize()
    await _bot_app.start()

    # На всякий случай чистим webhook и сбрасываем старые апдейты
    try:
        await _bot_app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logger.exception("[BOT] Не удалось удалить webhook: %s", e)

    # Запускаем polling через updater (PTB v21)
    if getattr(_bot_app, "updater", None) is not None:
        await _bot_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("[BOT] Polling запущен.")
        _bot_started = True
    else:
        logger.warning("[BOT] У Application нет updater — polling не запущен.")


async def stop_bot_polling() -> None:
    """
    Корректно останавливает бота при остановке приложения.
    """
    global _bot_app, _bot_started

    if _bot_app is None or not _bot_started:
        logger.info("[BOT] stop_bot_polling: бот уже остановлен или не запускался.")
        return

    if getattr(_bot_app, "updater", None) is not None:
        await _bot_app.updater.stop()

    await _bot_app.stop()
    await _bot_app.shutdown()

    _bot_started = False
    logger.info("[BOT] Остановлен.")







