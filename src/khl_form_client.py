# src/khl_form_client.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import hashlib
import random


@dataclass
class TeamForm:
    team_name: str
    games: int
    wins: int
    losses: int
    goals_for: float       # среднее забитых за матч
    goals_against: float   # среднее пропущенных за матч
    avg_total: float       # средний тотал (GF + GA)


def get_team_form(team_name: str) -> Optional[TeamForm]:
    ...

    """
    ВРЕМЕННАЯ ЗАГЛУШКА ДЛЯ ФОРМЫ КОМАНДЫ.

    ⚠️ ВАЖНО:
    - Сейчас здесь НЕТ реальных данных KHL.
    - Мы генерируем "похожую на правду" статистику, но детерминированно по названию команды.
      То есть для одной и той же команды всегда будут одни и те же цифры.
    - Когда решим, откуда брать реальные данные (парсер KHL.ru или платный API),
      просто меняем реализацию ЭТОЙ функции, не трогая остальной код.

    Интерфейс под будущую реальную интеграцию уже готов.
    """

    # нормализуем название команды, чтобы из него сделать seed
    name_norm = (team_name or "").strip().lower()
    if not name_norm:
        return None

    # детерминированный seed по имени команды
    seed = int(hashlib.md5(name_norm.encode("utf-8")).hexdigest(), 16) % (2**32)
    rnd = random.Random(seed)

    games = 5  # считаем форму по 5 последним матчам
    wins = rnd.randint(1, 4)
    losses = games - wins

    # "приблизительно правдоподобные" цифры
    goals_for = round(rnd.uniform(2.2, 3.8), 1)
    goals_against = round(rnd.uniform(1.8, 3.5), 1)
    avg_total = round(goals_for + goals_against, 1)

    return TeamForm(
        team_name=team_name,
        games=games,
        wins=wins,
        losses=losses,
        goals_for=goals_for,
        goals_against=goals_against,
        avg_total=avg_total,
    )
