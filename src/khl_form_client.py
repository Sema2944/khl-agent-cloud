# src/khl_form_client.py

"""
Базовый и расширенный сбор формы команд КХЛ.

TeamForm — то, что использует текущий бот (простая форма).
TeamAdvancedForm — расширенная версия для «про»-уровня модуля хоккейной аналитики.

Сейчас get_team_form — простая заглушка.
Позже добавим get_team_advanced_form с реальным парсингом KHL Lenta/API.
"""

from dataclasses import dataclass
from typing import Optional


# ============================================================
#   🔥 РАСШИРЕННАЯ СТАТИСТИКА ВРАТАРЕЙ
# ============================================================

@dataclass
class GoalieStats:
    """
    Расширенная статистика вратаря.
    Пока заполняем частично — источник данных появится позже.
    """
    name: str
    games: int = 0
    save_pct: Optional[float] = None      # процент отражённых, 0..100
    gaa: Optional[float] = None           # Goals Against Average
    last_5_save_pct: Optional[float] = None
    last_5_gaa: Optional[float] = None


# ============================================================
#   🔥 БАЗОВАЯ ФОРМА КОМАНДЫ — используем сейчас в боте
# ============================================================

@dataclass
class TeamForm:
    """
    Упрощённая форма команды, используется текущим ботом.

    Источник данных: пока заглушка.
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
#   🔥 РАСШИРЕННАЯ ФОРМА — для «PRO» хоккейного анализа
# ============================================================

@dataclass
class TeamAdvancedForm(TeamForm):
    """
    Расширенная форма команды.

    Наследуется от базового TeamForm, чтобы ничего не ломать.
    Добавляет продвинутые показатели: спецбригады, броски, физика, вратари.
    """

    # Дом/выезд и серии
    home_record: str = ""         # например: "8-2-0"
    away_record: str = ""         # например: "4-5-1"
    last_10_streak: str = ""      # "6-3-1"
    current_streak: str = ""      # "W3", "L2", "OTL1"

    # Спецбригады
    pp_pct: float = 0.0           # Power Play (%)
    pk_pct: float = 0.0           # Penalty Kill (%)

    # Темп и нагрузка
    shots_for: float = 0.0        # средние броски за матч
    shots_against: float = 0.0    # средние броски соперника
    penalties_per_game: float = 0.0
    hits_per_game: Optional[float] = None

    # Вратари
    main_goalie: Optional[GoalieStats] = None
    backup_goalie: Optional[GoalieStats] = None


# ============================================================
#   🔥 ФУНКЦИЯ ПОЛУЧЕНИЯ БАЗОВОЙ ФОРМЫ КОМАНДЫ
# ============================================================

async def get_team_form(team_name: str) -> Optional[TeamForm]:
    """
    ⚠️ Заглушка, которую использует текущий бот.
    Возвращает простую форму по команде.

    В PRO-версии появится get_team_advanced_form.
    """
    # Пока базовая заглушка
    # Реальные данные подгрузим позже через KHL API / парсер.
    return TeamForm(
        team_name=team_name,
        wins=5,
        losses=4,
        ot_losses=1,
        avg_goals_for=3.1,
        avg_goals_against=2.7,
        avg_total=5.8,
        games_last_2_days=0,
        games_last_3_days=1,
    )

