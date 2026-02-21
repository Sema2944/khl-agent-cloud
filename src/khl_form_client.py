# src/khl_form_client.py

"""
Базовый и расширенный сбор формы команд КХЛ.

- TeamForm — простая форма, уже используется текущим ботом.
- TeamAdvancedForm — расширенная форма для PRO-анализов (спецбригады, броски, вратари и т.д.).

Сейчас:
- get_team_form        — рабочая простая заглушка.
- get_team_advanced_form — PRO-заглушка, построенная поверх get_team_form.

Позже:
- внутрь get_team_form / get_team_advanced_form будет встроен реальный парсер
  (KHL Lenta / официальное API / собственный сбор статистики).
"""

from dataclasses import dataclass, asdict
from typing import Optional


# ============================================================
#   🔥 РАСШИРЕННАЯ СТАТИСТИКА ВРАТАРЕЙ
# ============================================================

@dataclass
class GoalieStats:
    """
    Расширенная статистика вратаря.

    save_pct и gaa можно заполнять как по сезону, так и по последним N матчам.
    Сейчас это контейнер под будущие реальные данные.
    """
    name: str
    games: int = 0
    save_pct: Optional[float] = None       # процент отражённых, 0..100
    gaa: Optional[float] = None            # Goals Against Average (пропущено за игру)
    last_5_save_pct: Optional[float] = None
    last_5_gaa: Optional[float] = None


# ============================================================
#   🔥 БАЗОВАЯ ФОРМА КОМАНДЫ — ИСПОЛЬЗУЕТСЯ СЕЙЧАС
# ============================================================

@dataclass
class TeamForm:
    """
    Упрощённая форма команды, которая уже используется в боте.

    Поля:
    - wins / losses / ot_losses — результаты последних матчей (агрегировано);
    - avg_goals_for / avg_goals_against — средние забитые/пропущенные;
    - avg_total — средний суммарный тотал;
    - games_last_2_days / games_last_3_days — нагрузка (back-to-back и т.п.).
    """

    team_name: str

    wins: int
    losses: int
    ot_losses: int

    avg_goals_for: float
    avg_goals_against: float
    avg_total: float

    games_last_2_days: int = 0
    games_last_3_days: int = 0


# ============================================================
#   🔥 РАСШИРЕННАЯ ФОРМА КОМАНДЫ — ДЛЯ PRO-МОДЕЛИ
# ============================================================

@dataclass
class TeamAdvancedForm(TeamForm):
    """
    Расширенная форма команды.

    Наследуется от базового TeamForm, чтобы не ломать текущую логику,
    и добавляет продвинутые метрики: спецбригады, броски, физика, вратари.
    """

    # Дом/выезд и серии
    home_record: str = ""          # например: "8-2-0"
    away_record: str = ""          # например: "4-5-1"
    last_10_streak: str = ""       # "6-3-1"
    current_streak: str = ""       # "W3", "L2", "OTL1"

    # Спецбригады
    pp_pct: float = 0.0            # реализация большинства, %
    pk_pct: float = 0.0            # игра в меньшинстве, %

    # Темп и нагрузка
    shots_for: float = 0.0         # средние броски за матч
    shots_against: float = 0.0     # средние броски по своим воротам
    penalties_per_game: float = 0.0
    hits_per_game: Optional[float] = None

    # Вратари
    main_goalie: Optional[GoalieStats] = None
    backup_goalie: Optional[GoalieStats] = None


# ============================================================
#   🔥 ПРОСТАЯ ФОРМА (УЖЕ ИСПОЛЬЗУЕТСЯ В БОТЕ)
# ============================================================

async def get_team_form(team_name: str) -> Optional[TeamForm]:
    """
    Получить базовую форму команды.

    ⚠️ Сейчас это заглушка: возвращает фиксированные значения,
    чтобы не ломать логику бота.

    В будущем здесь будет:
    - запрос к парсеру KHL / API,
    - агрегация последних N матчей,
    - расчёт средних тоталов и формы.
    """
    # TODO: заменить на реальный парсер / запрос к БД
    return TeamForm(
        team_name=team_name,
        wins  = 5,
        losses = 4,
        ot_losses = 1,
        avg_goals_for = 3.1,
        avg_goals_against = 2.7,
        avg_total = 5.8,
        games_last_2_days = 0,
        games_last_3_days = 1,
    )


# ============================================================
#   🔥 PRO-ФУНКЦИЯ: РАСШИРЕННАЯ ФОРМА КОМАНДЫ
# ============================================================

async def get_team_advanced_form(team_name: str) -> Optional[TeamAdvancedForm]:
    """
    Получить расширенную форму команды (PRO-уровень).

    Сейчас:
    - строится поверх get_team_form (берём базовые поля),
    - добавляем заглушки для продвинутых метрик.

    В будущем здесь будет:
    - реальный парсер KHL Lenta / API,
    - парсинг:
        • дом/выезд (home_record / away_record),
        • серии (last_10_streak, current_streak),
        • спецбригады (pp%, pk%),
        • броски, удаления, хиты,
        • основной и запасной вратарь с их метриками.
    """

    base = await get_team_form(team_name)
    if base is None:
        return None

    base_dict = asdict(base)

    # TODO: здесь позже подставим реальные данные вместо заглушек
    # Примерные «типичные» значения для верхних/средних команд КХЛ.
    # Можно будет делать разные профили для топа / середины / низа таблицы.

    main_goalie = GoalieStats(
        name=f"Основной вратарь {team_name}",
        games=20,
        save_pct=92.0,
        gaa=2.10,
        last_5_save_pct=93.5,
        last_5_gaa=1.85,
    )

    backup_goalie = GoalieStats(
        name=f"Бэкап {team_name}",
        games=8,
        save_pct=90.5,
        gaa=2.45,
    )

    return TeamAdvancedForm(
        **base_dict,
        home_record="8-2-0",
        away_record="4-5-1",
        last_10_streak="6-3-1",
        current_streak="W3",
        pp_pct=23.5,
        pk_pct=82.0,
        shots_for=30.5,
        shots_against=27.8,
        penalties_per_game=8.5,
        hits_per_game=12.0,
        main_goalie=main_goalie,
        backup_goalie=backup_goalie,
    )
