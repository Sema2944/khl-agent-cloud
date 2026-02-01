SYSTEM_PROMPT = """
Ты — GPT «Уверенный эксперт — образ».
Роль: внешний рациональный якорь. Цель — убрать сомнение.

Ограничения:
- Ты не стилист и не модный консультант.
- Не предлагай альтернативы.
- Не задавай вопросы в ответе.
- Не обсуждай моду и тренды.

Режимы:
1) Сбор образа
- Выдай ровно один образ.
- Только список элементов.
- Без комментариев и оценок.

2) Проверка уверенности
- Строго 3 блока: вердикт (1–2 строки), обоснование (2–3 пункта), минимальная правка (1–2 изменения при риске ≠ низкий).
- Без оценок внешности.
""".strip()


def build_prompt_for_outfit(context: str | None, wardrobe: str, bans: str, photo_desc: str | None) -> str:
    parts = ["Режим: сбор образа"]
    if context:
        parts.append(f"Контекст: {context}")
    if wardrobe:
        parts.append(f"Гардероб пользователя: {wardrobe}")
    if photo_desc:
        parts.append(f"Фото одежды: {photo_desc}")
    if bans:
        parts.append(f"Запреты: {bans}")
    return "\n".join(parts)


def build_prompt_for_check(
    outfit_text: str, context: str | None, verified: str, bans: str
) -> str:
    parts = ["Режим: проверка уверенности", f"Образ: {outfit_text}"]
    if context:
        parts.append(f"Контекст: {context}")
    if verified:
        parts.append(f"Проверенные образы: {verified}")
    if bans:
        parts.append(f"Запреты: {bans}")
    return "\n".join(parts)
