# src/hockey_model.py

"""
Хоккейная модель: базовые структуры и подсчёты.

Задача модуля:
- принять сырые данные по форме команд, вратарям, тренерам;
- сделать "снимок силы" команды;
- сделать high-level разбор матчапа (тотал, темп, перекос по 1X2, риск апсета);
- помочь с оценкой value (связка с коэффициентами).

Сейчас:
- build_team_strength_from_form           — базовый снимок силы по TeamForm;
- build_team_strength_from_advanced_form  — PRO-снимок силы по TeamAdvancedForm;
- build_matchup_view                      — агрегированный разбор матчапа.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

from .khl_form_client import TeamForm, TeamAdvancedForm, GoalieStats


# ---------- БАЗОВЫЕ СТРУКТУРЫ ДЛЯ КОМАНДЫ ----------


@dataclass
class GoalieInfo:
    """
    Информация по вратарю (упрощённо).

    TODO: когда появится отдельный источник данных, сюда можно добавить:
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

    Все числовые поля — условно нормированы (0..100) или понятные метрики,
    которые потом можно крутить в модельках.
    """

    team_name: str

    # Атака / оборона
    offense: float          # 0..100
    defense: float          # 0..100

    # Спецбригады
    pp_strength: float      # power play (0..100)
    pk_strength: float      # penalty killing (0..100)

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

    # кто фаворит по модели (без учёта коэффициентов)
    model_category: str              # "равный", "легкий фаворит", "явный фаворит"
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
    avg_gf = form.avg_goals_for
    avg_ga = form.avg_goals_against

    # очень грубая шкала, просто чтобы были различия
    offense = max(0.0, min(100.0, avg_gf * 15.0))           # 3.0 гола → 45
    defense = max(0.0, min(100.0, (4.0 - avg_ga) * 25.0))   # чем меньше пропускает, тем выше

    # темп: ориентируемся на суммарный тотал
    total = form.avg_total
    pace = max(0.0, min(100.0, (total - 4.0) * 25.0))       # тоталы ближе к 6 → высокий темп

    # физика/удаления пока не считаем → заглушка
    physicality = 50.0

    # спецбригады — заглушки до появления данных
    pp_strength = 50.0
    pk_strength = 50.0

    # вратарь
    if goalie and goalie.save_pct is not None:
        goalie_score = max(0.0, min(100.0, (goalie.save_pct - 88.0) * 5.0))
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
    if getattr(form, "ot_losses", 0):
        record += f"-{form.ot_losses}"

    # усталость (back-to-back и т.п.) пока просто заглушка
    fatigue_score = 20.0 if getattr(form, "games_last_3_days", 0) >= 2 else 0.0
    is_b2b = getattr(form, "games_last_2_days", 0) >= 2

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


def _goalie_to_score(g: Optional[GoalieStats]) -> float:
    """
    Примерный маппинг статистики вратаря в шкалу 0..100.
    Всё очень грубо, но даёт сигнал: топовый кипер vs середняк.
    """
    if g is None or g.save_pct is None:
        return 50.0

    base = (g.save_pct - 88.0) * 6.0  # 90% → ~12, 92% → ~24, 94% → ~36
    if g.last_5_save_pct is not None:
        delta = g.last_5_save_pct - g.save_pct   # форма vs сезон
    else:
        delta = 0.0

    score = base + delta * 2.0
    return max(0.0, min(100.0, score))


def build_team_strength_from_advanced_form(
    team_name: str,
    form: TeamAdvancedForm,
) -> TeamStrengthSnapshot:
    """
    PRO-версия: используем TeamAdvancedForm — спецбригады, броски, физику, вратарей.

    Логика:
    - берём базовый снимок через build_team_strength_from_form;
    - усиливаем/корректируем поля на основе расширенной статистики.
    """

    base = build_team_strength_from_form(team_name, form)

    # Атака: заброшенные + броски в створ
    offense = base.offense
    offense += max(-10.0, min(10.0, (form.shots_for - 28.0) * 0.8))
    offense = max(0.0, min(100.0, offense))

    # Оборона: пропущенные + сколько бросков по своим воротам
    defense = base.defense
    defense += max(-10.0, min(10.0, (30.0 - form.shots_against) * 0.7))
    defense = max(0.0, min(100.0, defense))

    # Спецбригады: pp/pk в процентах сразу мапим в 0..100 с лёгким сглаживанием
    pp_strength = max(0.0, min(100.0, form.pp_pct))
    pk_strength = max(0.0, min(100.0, form.pk_pct))

    # Темп: суммарные броски дают хороший сигнал, плюс немного от удалений
    total_shots = form.shots_for + form.shots_against
    pace = (total_shots - 50.0) * 2.0 + form.penalties_per_game * 1.0
    pace = max(0.0, min(100.0, pace))

    # Физика: удаления + хиты
    phys = form.penalties_per_game * 3.0
    if form.hits_per_game is not None:
        phys += form.hits_per_game * 2.0
    physicality = max(0.0, min(100.0, phys))

    # Вратарь: берём основного, если есть, иначе бэкап
    main_or_backup = form.main_goalie or form.backup_goalie
    goalie_score = _goalie_to_score(main_or_backup)

    # Усталость пока оставим как в базовом снимке
    fatigue_score = base.fatigue_score
    is_b2b = base.is_back_to_back

    # last_10_record — если в форме есть строка last_10_streak, используем её
    last_10_record = form.last_10_streak or base.last_10_record

    return TeamStrengthSnapshot(
        team_name=team_name,
        offense=offense,
        defense=defense,
        pp_strength=pp_strength,
        pk_strength=pk_strength,
        pace=pace,
        physicality=physicality,
        goalie_score=goalie_score,
        coach_aggressiveness=base.coach_aggressiveness,
        last_10_record=last_10_record,
        avg_goals_for=form.avg_goals_for,
        avg_goals_against=form.avg_goals_against,
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
    diff = score1 - score2
    gap = abs(diff)

    if gap < 8:
        edge_side = "even"
        model_category = "равный матч"
        edge_conf = 40.0
    elif gap < 20:
        edge_side = "team1" if diff > 0 else "team2"
        model_category = "легкий фаворит"
        edge_conf = 55.0 + gap / 3.0
    else:
        edge_side = "team1" if diff > 0 else "team2"
        model_category = "явный фаворит"
        edge_conf = 70.0 + min(20.0, (gap - 20.0) / 2.0)

    edge_conf = max(40.0, min(95.0, edge_conf))

    # темп и тоталы
    avg_pace = (team1.pace + team2.pace) / 2.0
    if avg_pace >= 65.0:
        pace_comment = "Матч выглядит верховым по темпу: много бросков, высокий тотал."
        total_comment = "Скорее склонение в сторону тоталов 'больше'."
    elif avg_pace <= 35.0:
        pace_comment = "Матч просится в низовой: команды играют аккуратно и без безумного темпа."
        total_comment = "Логика ближе к тоталам 'меньше', но смотри на спецбригады и вратарей."
    else:
        pace_comment = "Темп ближе к среднему, явного сигнала по тоталам нет."
        total_comment = "Тотал лучше решать по конкретной линии и составам."

    # физика
    avg_phys = (team1.physicality + team2.physicality) / 2.0
    if avg_phys >= 65.0:
        phys_comment = "Ожидается жёсткий матч с большим количеством борьбы и удалений."
    elif avg_phys <= 35.0:
        phys_comment = "Скорее аккуратный хоккей без лишней грязи."
    else:
        phys_comment = "По жёсткости матч ближе к среднему уровню."

    # дуэль вратарей
    if abs(team1.goalie_score - team2.goalie_score) < 10.0:
        goalie_comment = "По вратарям перекос небольшой — дуэль ближе к равной."
    elif team1.goalie_score > team2.goalie_score:
        goalie_comment = f"Плюс по вратарю на стороне {team1.team_name}."
    else:
        goalie_comment = f"Плюс по вратарю на стороне {team2.team_name}."

    # риск апсета
    if gap < 8.0:
        upset_level = "высокий"
        upset_comment = "Модель видит матч близким, апсет вполне реален."
    elif gap < 20.0:
        upset_level = "средний"
        upset_comment = "Фаворит есть, но у андердога достаточно ресурса, чтобы устроить сюрприз."
    else:
        upset_level = "низкий"
        upset_comment = "Сильный фаворит, апсет возможен только при провале лидера."

    return MatchupView(
        team1=team1.team_name,
        team2=team2.team_name,
        model_category=model_category,
        model_edge_side=edge_side,
        model_edge_confidence=edge_conf,
        expected_pace_comment=pace_comment,
        total_hint_comment=total_comment,
        physicality_comment=phys_comment,
        goalie_duel_comment=goalie_comment,
        upset_risk_level=upset_level,
        upset_risk_comment=upset_comment,
    )
