import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.db import init_db, close_db
from src.telegram_bot import start_bot, stop_bot

# -----------------------
#   НАСТРОЙКА ЛОГИРОВАНИЯ
# -----------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

log = logging.getLogger("svc")

# -----------------------
#        FASTAPI APP
# -----------------------

app = FastAPI(
    title="KHL Agent API",
    version="1.0.0",
)

# Если нужен CORS (можно убрать, если не используешь из браузера)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------
#       LIFECYCLE
# -----------------------

@app.on_event("startup")
async def on_startup() -> None:
    """
    Старт сервиса:
    - инициализируем БД
    - запускаем Telegram-бота (polling)
    """
    log.info("[APP] Запуск приложения...")
    await init_db()
    await start_bot()
    log.info("[APP] Приложение запущено.")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    """
    Остановка сервиса:
    - останавливаем Telegram-бота
    - закрываем БД
    """
    log.info("[APP] Остановка приложения...")
    await stop_bot()
    await close_db()
    log.info("[APP] Приложение остановлено.")


# -----------------------
#        РОУТЫ API
# -----------------------

@app.get("/")
async def root():
    """
    Простейший эндпоинт, чтобы Render понимал, что сервис жив.
    """
    return {"status": "ok", "service": "khl-agent-api"}


@app.get("/health")
async def health():
    """
    Эндпоинт для проверки здоровья сервиса.
    """
    return {"status": "ok"}


