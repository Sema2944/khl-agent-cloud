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

from .khl_form_client import TeamForm  # уже используем в service.py


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
    avg_total: float

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


# ---------- ВСПОМОГАТЕЛЬНОЕ ----------


def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


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

    # Берём поля максимально безопасно, чтобы не падать, если чего-то нет
    avg_gf = float(getattr(form, "avg_goals_for", 0.0) or 0.0)
    avg_ga = float(getattr(form, "avg_goals_against", 0.0) or 0.0)

    avg_total = getattr(form, "avg_total", None)
    if avg_total is None:
        avg_total = avg_gf + avg_ga
    avg_total = float(avg_total or 0.0)

    wins = int(getattr(form, "wins", 0) or 0)
    losses = int(getattr(form, "losses", 0) or 0)
    ot_losses = int(getattr(form, "ot_losses", 0) or 0)

    # атака / оборона (очень грубые шкалы)
    offense = _clip(avg_gf * 15.0)          # 3.0 гола → 45
    defense = _clip((4.0 - avg_ga) * 25.0)  # чем меньше пропускает, тем выше

    # темп по тоталам
    pace = _clip((avg_total - 4.0) * 25.0)  # 6 тотал → высокий темп

    # физика/удаления пока не считаем → заглушка
    physicality = 50.0

    # спецбригады — заглушки до появления реальных данных
    pp_strength = 50.0
    pk_strength = 50.0

    # вратарь
    if goalie and goalie.save_pct is not None:
        goalie_score = _clip((goalie.save_pct - 88.0) * 5.0)
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
    record = f"{wins}-{losses}"
    if ot_losses:
        record += f"-{ot_losses}"

    # усталость (back-to-back и т.п.) — через getattr с дефолтом
    games_last_3_days = int(getattr(form, "games_last_3_days", 0) or 0)
    games_last_2_days = int(getattr(form, "games_last_2_days", 0) or 0)

    fatigue_score = 20.0 if games_last_3_days >= 2 else 0.0
    is_b2b = games_last_2_days >= 2

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
        avg_total=avg_total,
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
        edge_conf = min(90.0, 50.0 + (score1 - score2) / 3.0)
    else:
        edge_side = "team2"
        edge_conf = min(90.0, 50.0 + (score2 - score1) / 3.0)

    # темп и тоталы
    avg_pace = (team1.pace + team2.pace) / 2.0
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
    avg_phys = (team1.physicality + team2.physicality) / 2.0
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
