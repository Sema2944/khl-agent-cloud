# src/parsing.py
from __future__ import annotations

import os
import logging
from typing import Any

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# Клиент OpenAI. Ключ берём из переменной окружения OPENAI_API_KEY
client = AsyncOpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
)

# Модель можно переопределить через переменную окружения OPENAI_MODEL
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

SYSTEM_PROMPT = """
Ты — AI-агент для ставок на хоккей (КХЛ).
Общайся по-русски, дружелюбно и по делу.

У тебя есть бэкенд с базой ставок и статистикой, 
но сейчас ты обращаешься к нему косвенно — через текстовые команды, которые понимает сервер.

Если пользователь пишет:
- "профиль" — расскажи, что ты можешь показать его статистику, банк, историю ставок.
- "мои ставки" — объясни, что можно добавлять и размечать ставки.
- "КХЛ сегодня" — расскажи, что можешь подсказать по матчам сегодняшнего дня.
- Любой другой текст — просто помоги по теме ставок/хоккея/банка.

Если чего-то сделать технически нельзя, честно говори об этом.
Не придумывай данные о реальных матчах и ставках, которых у тебя нет.
"""

async def run_dialog_agent(user_id: int, message: str) -> str:
    """
    Главная функция диалогового агента, которую импортирует service.py.

    Сейчас это простая обёртка над LLM без инструментов.
    Позже сюда можно вернуть сложную логику с tools / базой.
    """
    logger.info("run_dialog_agent: user_id=%s, message=%r", user_id, message)

    # Мини-костыль: если совсем пустая строка
    if not message.strip():
        return "Напиши мне что-нибудь про ставки или КХЛ 🙂"

    try:
        resp = await client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"[user_id: {user_id}] {message}",
                },
            ],
            temperature=0.3,
        )
        content = resp.choices[0].message.content
        return content or "Я слегка потерялся, попробуй сформулировать вопрос иначе 🙂"
    except Exception as e:
        logger.exception("Ошибка при вызове OpenAI")
        return f"⚠️ Ошибка LLM: {type(e).__name__}: {e}"
