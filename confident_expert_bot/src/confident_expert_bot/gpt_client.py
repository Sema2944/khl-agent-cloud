from __future__ import annotations

from openai import AsyncOpenAI

from confident_expert_bot.prompts import SYSTEM_PROMPT


class GptClient:
    def __init__(self, api_key: str, model: str = "gpt-4o-mini") -> None:
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    async def complete(self, user_prompt: str) -> str:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
        )
        return response.choices[0].message.content.strip()

    async def describe_photos(self, urls: list[str]) -> str:
        if not urls:
            return ""
        content = [
            {
                "type": "text",
                "text": (
                    "Опиши одежду на фото нейтрально и структурированно. "
                    "Формат: верх / низ / обувь / аксессуары / верхняя одежда / цвета. "
                    "Без предположений и брендов."
                ),
            }
        ]
        content.extend({"type": "image_url", "image_url": {"url": url}} for url in urls)
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": "Ты описываешь одежду на фото без догадок."},
                {"role": "user", "content": content},
            ],
            temperature=0,
        )
        return response.choices[0].message.content.strip()
