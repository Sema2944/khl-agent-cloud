# src/hockey_model.py

"""
Хоккейная модель: базовые структуры и подсчёты.

Задача модуля:
- принять сырые данные по форме команд, вратарям, тренерам;
- сделать "снимок силы" команды;
- сделать high-level разбор матчапа (тотал, темп, перекос по 1X2, риск апсета);
- помочь с оценкой value (связка с коэффициентами).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .khl_form_client import TeamForm  # мы это уже используем в service.py


# ---------- БАЗОВЫЕ СТРУКТУРЫ ДЛЯ КОМАНДЫ ----------


@dataclass
class GoalieInfo:
    """
    Информация по вратарю (упрощённо).

    TODO: когда появится источник данных, сюда можно добавить:
    - save% (общий, по последним матчам)
    - GAA
    - нагрузка (back-to-back)
    - домашняя/выездная эффективность и т.д.
    """
    name: str
    is_main: bool = True
    save_pct: Optional[float] = None  # 0..100
    gaa: Optional[float] = None       # goals against average


@dataclass
class CoachProfile:
    """
    Профиль тренера / игровой модели.
    Пока — ручные теги, потом можно научиться вытаскивать автоматически.
    """
    name: str
    style: str  # "атакующий", "закрытый", "баланс"
    risk_level: str  # "низкий", "средний", "высокий"


@dataclass
class TeamStrengthSnapshot:
    """
    Снимок силы команды на сейчас.

    Все поля — нормированные (0..100) или понятные метрики,
    которые потом можно крутить в модельках.
    """

    team_name: str

    # Атака / оборона
    offense: float          # 0..100
    defense: float          # 0..100

    # Спецбригады
    pp_strength: float      # power play
    pk_strength: float      # penalty killing

    # Темп и стиль
    pace: float             # 0..100 (чем выше, тем более "over" команда)
    physicality: float      # 0..100 (жёсткость, удаления, борьба)

    # Вратарь и тренер
    goalie_score: float     # 0..100
    coach_aggressiveness: float  # 0..100

    # Форма
    last_10_record: str     # например '6-3-1'
    avg_goals_for: float
    avg_goals_against: float

    # Ситуационные факторы
    is_back_to_back: bool = False
    fatigue_score: float = 0.0   # 0..100


@dataclass
class MatchupView:
    """
    Разбор матчапа двух команд.
    Это то, что мы будем конвертировать в человеческий текст для бота.
    """

    team1: str
    team2: str

    # кто фаворит по модели (без коэффициентов)
    model_edge_side: str             # "team1", "team2" или "even"
    model_edge_confidence: float     # 0..100

    # Темп и тоталы
    expected_pace_comment: str
    total_hint_comment: str

    # Стиль матча
    physicality_comment: str
    goalie_duel_comment: str

    # Риск апсета (андердога)
    upset_risk_level: str           # "низкий", "средний", "высокий"
    upset_risk_comment: str


# ---------- ФУНКЦИИ ПРЕОБРАЗОВАНИЯ ДАННЫХ В "СИЛУ КОМАНДЫ" ----------


def build_team_strength_from_form(
    team_name: str,
    form: TeamForm,
    goalie: Optional[GoalieInfo] = None,
    coach: Optional[CoachProfile] = None,
) -> TeamStrengthSnapshot:
    """
    Грубая версия: берём TeamForm (что уже есть по последним матчам)
    и конвертируем в нормированный снимок силы.

    Сейчас — очень упрощённо, как заглушка. Потом можно добавить:
    - реальную нормализацию по лиге,
    - веса для последних матчей,
    - влияние вратаря и тренера.
    """

    # атака / оборона: нормализуем средний забитый/пропущенный тотал
    # (просто грубая шкала, потом заменим на что-то умнее)
    avg_gf = form.avg_goals_for
    avg_ga = form.avg_goals_against

    offense = max(0.0, min(100.0, avg_gf * 15))   # грубо: 3.0 гола → 45
    defense = max(0.0, min(100.0, (4.0 - avg_ga) * 25))  # чем меньше пропускает, тем выше

    # темп: ориентируемся на суммарный тотал
    total = form.avg_total
    pace = max(0.0, min(100.0, (total - 4.0) * 25))  # тоталы ближе к 6 → высокий темп

    # физика/удаления пока не считаем → заглушка
    physicality = 50.0

    # спецбригады — заглушки до появления данных
    pp_strength = 50.0
    pk_strength = 50.0

    # вратарь
    if goalie and goalie.save_pct is not None:
        goalie_score = max(0.0, min(100.0, (goalie.save_pct - 88.0) * 5))
    else:
        goalie_score = 50.0

    # тренер
    if coach:
        if coach.style == "атакующий":
            coach_aggr = 70.0
        elif coach.style == "закрытый":
            coach_aggr = 30.0
        else:
            coach_aggr = 50.0
    else:
        coach_aggr = 50.0

    # форма в читаемый вид
    record = f"{form.wins}-{form.losses}"
    if form.ot_losses:
        record += f"-{form.ot_losses}"

    # усталость (back-to-back и т.п.) пока просто заглушка
    fatigue_score = 20.0 if form.games_last_3_days >= 2 else 0.0
    is_b2b = form.games_last_2_days >= 2

    return TeamStrengthSnapshot(
        team_name=team_name,
        offense=offense,
        defense=defense,
        pp_strength=pp_strength,
        pk_strength=pk_strength,
        pace=pace,
        physicality=physicality,
        goalie_score=goalie_score,
        coach_aggressiveness=coach_aggr,
        last_10_record=record,
        avg_goals_for=avg_gf,
        avg_goals_against=avg_ga,
        is_back_to_back=is_b2b,
        fatigue_score=fatigue_score,
    )


# ---------- ФУНКЦИЯ РАЗБОРА МАТЧАПА ----------


def build_matchup_view(
    team1: TeamStrengthSnapshot,
    team2: TeamStrengthSnapshot,
) -> MatchupView:
    """
    На основе двух 'снимков силы' формируем структурированный разбор.
    Это ядро для текста типа:

    - 'ожидается верховой матч'
    - 'фаворит выглядит сильнее по атаке, но вратарь на стороне андердога'
    - 'высокий риск апсета'
    """

    # кто фаворит по модели (очень грубо: сумма offense+defense+goalie)
    score1 = team1.offense + team1.defense + team1.goalie_score
    score2 = team2.offense + team2.defense + team2.goalie_score

    if abs(score1 - score2) < 10:
        edge_side = "even"
        edge_conf = 40.0
    elif score1 > score2:
        edge_side = "team1"
        edge_conf = min(90.0, 50.0 + (score1 - score2) / 3)
    else:
        edge_side = "team2"
        edge_conf = min(90.0, 50.0 + (score2 - score1) / 3)

    # темп и тоталы
    avg_pace = (team1.pace + team2.pace) / 2
    if avg_pace >= 65:
        pace_comment = "Матч выглядит верховым по темпу: много бросков, высокий тотал."
        total_comment = "Скорее склонение в сторону тоталов 'больше'."
    elif avg_pace <= 35:
        pace_comment = "Матч просится в низовой: команды играют аккуратно и без безумного темпа."
        total_comment = "Логика ближе к тоталам 'меньше', но смотри на спецбригады и вратарей."
    else:
        pace_comment = "Темп ближе к среднему, явного сигнала по тоталам нет."
        total_comment = "Тотал лучше решать по конкретной линии и составам."

    # физика
    avg_phys = (team1.physicality + team2.physicality) / 2
    if avg_phys >= 65:
        phys_comment = "Ожидается жёсткий матч с удалениями и борьбой."
    elif avg_phys <= 35:
        phys_comment = "Скорее аккуратный хоккей без лишней грязи."
    else:
        phys_comment = "По жёсткости матч ближе к среднему уровню."

    # дуэль вратарей
    if abs(team1.goalie_score - team2.goalie_score) < 10:
        goalie_comment = "По вратарям заметного перекоса нет — дуэль ближе к равной."
    elif team1.goalie_score > team2.goalie_score:
        goalie_comment = f"Плюс по вратарю на стороне {team1.team_name}."
    else:
        goalie_comment = f"Плюс по вратарю на стороне {team2.team_name}."

    # риск апсета
    # логика: если фаворит по модели, но у андердога неплохая атака/темп — риск выше
    diff = abs(score1 - score2)
    if diff < 10:
        upset_level = "высокий"
        upset_comment = "Модель видит матч близким, апсет вполне реален."
    elif diff < 25:
        upset_level = "средний"
        upset_comment = "Фаворит есть, но у андердога достаточно ресурса, чтобы устроить сюрприз."
    else:
        upset_level = "низкий"
        upset_comment = "Сильный фаворит, апсет возможен только при провале лидера."

    return MatchupView(
        team1=team1.team_name,
        team2=team2.team_name,
        model_edge_side=edge_side,
        model_edge_confidence=edge_conf,
        expected_pace_comment=pace_comment,
        total_hint_comment=total_comment,
        physicality_comment=phys_comment,
        goalie_duel_comment=goalie_comment,
        upset_risk_level=upset_level,
        upset_risk_comment=upset_comment,
    )
