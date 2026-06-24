# Confident Expert — Outfit Bot

Telegram bot интерфейс для GPT «Уверенный эксперт — образ». Бот управляет режимами, собирает входные данные (текст + фото) и возвращает ответ GPT без интерпретаций.

## Быстрый старт

1. Создайте `.env` рядом с `pyproject.toml`:

```
TELEGRAM_BOT_TOKEN=your-token
OPENAI_API_KEY=your-openai-key
ADMIN_IDS=123456789,987654321
DATABASE_PATH=./data/bot.db
S3_BUCKET=confident-expert-bot
S3_REGION=eu-central-1
S3_ACCESS_KEY_ID=your-access-key
S3_SECRET_ACCESS_KEY=your-secret-key
S3_ENDPOINT_URL=https://s3.your-provider.com
```

2. Установите зависимости:

```
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

3. Запустите бота:

```
confident-expert-bot
```

## Архитектура (MVP)

- `confident_expert_bot.main` — роутинг Telegram, состояние диалога, вызовы GPT.
- `confident_expert_bot.storage` — SQLite-хранилище пользователей, сессий, фото.
- `confident_expert_bot.gpt_client` — генерация промптов и вызовы OpenAI API (включая vision).
- `confident_expert_bot.s3_client` — загрузка фото в S3 и выдача временных ссылок.

## ТЗ (ключевые ограничения)

- Бот не интерпретирует ответ GPT и не задаёт вопросы после финального ответа.
- Режимы строго разделены: `Сбор образа` и `Проверка уверенности`.
- Авторизация по Telegram user_id (whitelist), админ-команды через `/add_user`.
- Фото принимаются до 6 штук, сохраняются как file_id и metadata.
