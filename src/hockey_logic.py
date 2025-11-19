# src/hockey_logic.py

"""
Хоккейная "контекстная" логика:
- турнирный контекст (кто за что борется);
- мотивация;
- споты календаря (back-to-back, 3in4, важный следующий матч);
- объяснение паттерна, когда топ может недоигрывать с аутсайдером
  перед более важным соперником.

Задача модуля сейчас:
- дать человеку понятный чек-лист по мотивации;
- сформировать текст, который мы вставляем в разбор матча;
- держать аккуратный каркас под будущие реальные данные (таблица, календарь).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ---------- БАЗОВЫЕ СТРУКТУРЫ ДЛЯ ТАБЛИЦЫ И КАЛЕНДАРЯ ----------


@dataclass
class TeamStandingSummary:
    """
    Краткое состояние команды в таблице.

    Поля сделаны с запасом — сейчас мы их не заполняем из реального API,
    но позже можно будет подключить источник и передавать сюда реальные значения.
    """

    team_name: str

    # место в конференции / лиге
    rank: Optional[int] = None

    # отрыв от ближайших:
    # (значения в очках, могут быть отрицательными, если команда впереди)
    points_to_next_above: Optional[int] = None   # до ближайшей команды выше
    points_to_next_below: Optional[int] = None   # до ближайшей команды ниже

    # Боится ли вылететь из плей-офф / борется ли за топ-посев
    is_fighting_for_playoff: Optional[bool] = None
    is_secure_playoff_team: Optional[bool] = None
    is_top_team: Optional[bool] = None           # топ-4 условно
    is_bottom_team: Optional[bool] = None        # дно таблицы


@dataclass
class ScheduleSpot:
    """
    Позиция команды в календаре.

    Это то место, куда отлично ложится твоя логика:
    - "топ играет со слабым сейчас, а через день — с прямым конкурентом";
    - back-to-back, 3 матча за 4 дня, перегрузка и т.п.
    """

    team_name: str

    # Нагрузка
    is_back_to_back: bool = False     # играет второй день подряд
    is_3in4: bool = False             # 3 матча за 4 дня
    is_4in6: bool = False             # 4 матча за 6 дней

    # Важность следующего матча
    next_opponent: Optional[str] = None
    next_opponent_is_direct_rival: bool = False  # прямой конкурент по таблице
    next_game_is_very_important: bool = False    # условный "матч за 4 очка"


# ---------- ВНУТРЕННИЙ ХЕЛПЕР: ОБЪЯСНЕНИЕ "МЯГКОГО" МАТЧА ДЛЯ ТОПА ----------


def _describe_soft_spot_for_favourite(
    fav: TeamStandingSummary | None,
    dog: TeamStandingSummary | None,
    fav_spot: ScheduleSpot | None,
) -> Optional[str]:
    """
    Описывает ситуацию, когда у фаворита может быть 'мягкий' матч:
    - фаворит из верхней части таблицы,
    - соперник — явный низ,
    - ближайший матч у фаворита с прямым конкурентом → возможен недо-настрой.
    """

    if fav is None or dog is None:
        return None

    # Нужны минимальные признаки: фаворит топ, соперник - низ.
    if not (fav.is_top_team and dog.is_bottom_team):
        return None

    lines: list[str] = []
    lines.append(
        "Есть классический сценарий для сильных команд: "
        "матч с аутсайдером иногда играется аккуратнее, "
        "если на горизонте более важный соперник."
    )

    if fav_spot and (fav_spot.next_game_is_very_important or fav_spot.next_opponent_is_direct_rival):
        # Привязываем к 'следующему матчу', если данные есть
        if fav_spot.next_opponent:
            lines.append(
                f"{fav.team_name} может частично экономить силы на фоне "
                f"ожидания более важного матча против {fav_spot.next_opponent}."
            )
        else:
            lines.append(
                f"{fav.team_name} может частично экономить силы, "
                "потому что следующий матч серьёзнее по турнирному значению."
            )

        lines.append(
            "В таких спотах часто видим: "
            "более ровное распределение игрового времени, "
            "меньше риска, допускают лишние моменты в защите."
        )
    else:
        lines.append(
            "Даже без явного 'матча за 4 очка' впереди, "
            "топ может сыграть прагматично: забрать свои очки малой кровью, "
            "без убийственного темпа все 60 минут."
        )

    lines.append(
        "Это не означает, что фаворит обязан 'слить' игру, "
        "но повышает риск недо-оценки аутсайдера и нервного матча."
    )

    return "\n".join(lines)


# ---------- ОСНОВНАЯ ФУНКЦИЯ ДЛЯ SERVICE.PY ----------


def build_match_context_notes(
    team1_name: str,
    team2_name: str,
    league: str = "KHL",
    team1_standing: Optional[TeamStandingSummary] = None,
    team2_standing: Optional[TeamStandingSummary] = None,
    team1_spot: Optional[ScheduleSpot] = None,
    team2_spot: Optional[ScheduleSpot] = None,
) -> str:
    """
    Собирает текстовый блок 'турнирный контекст / мотивация' для разбора матча.

    Сейчас:
    - если нет реальных standing/spot-данных → даём общий чек-лист по мотивации
      + объясняем паттерн 'топ vs низ + важный матч дальше', о котором ты говорил;
    - если когда-нибудь начнём прокидывать реальные standing/spot,
      сюда легко докрутим конкретику.
    """

    lines: list[str] = []

    # 0. Заголовок внутри блока уже добавляет вызывающая функция,
    # здесь пишем только содержимое.

    # 1. Общий чек-лист по мотивации и таблице
    lines.append(
        "Перед ставкой по такому матчу полезно посмотреть на турнирный контекст, "
        "а не только на коэффициенты."
    )
    lines.append("")
    lines.append("Чек-лист по мотивации:")
    lines.append("• Кто за что борется: за плей-офф, за топ-посев, чтобы не вылететь из зоны топ-8.")
    lines.append("• Есть ли у одной из команд комфортный запас очков перед преследователями.")
    lines.append("• Насколько критично каждой команде брать очки именно в этом матче.")

    # 2. Объясняем твой сценарий: топ + слабый соперник + важный матч впереди
    lines.append("")
    lines.append(
        "Отдельно стоит сценарий, когда топ-команда играет с явным аутсайдером, "
        "а через матч или сразу после — более важная игра с прямым конкурентом."
    )
    lines.append(
        "Логика клубов часто такая: слабый соперник всё равно не догонит в таблице, "
        "а вот прямой конкурент сверху/снизу — может. Поэтому "
        "иногда топ играет 'на полноги', экономит состав и рискует отдать очки аутсайдеру."
    )

    # 3. Если когда-нибудь начнём прокидывать реальные standing/spot — используем их
    soft_spot_notes: list[str] = []

    if team1_standing and team2_standing:
        # Попробуем выбрать фаворита/андердога по таблице
        fav = None
        dog = None
        fav_spot = None

        if team1_standing.is_top_team and team2_standing.is_bottom_team:
            fav, dog, fav_spot = team1_standing, team2_standing, team1_spot
        elif team2_standing.is_top_team and team1_standing.is_bottom_team:
            fav, dog, fav_spot = team2_standing, team1_standing, team2_spot

        soft = _describe_soft_spot_for_favourite(fav, dog, fav_spot)
        if soft:
            soft_spot_notes.append("")
            soft_spot_notes.append(soft)

    # 4. Если реальных standing/spot нет — даём человеку понятную инструкцию, что смотреть
    if not soft_spot_notes:
        lines.append("")
        lines.append("Как руками проверить риск 'мягкого' матча у фаворита:")
        lines.append(
            f"• Открой таблицу {league} и посмотри, нет ли у {team1_name} или {team2_name} "
            "большого запаса очков над зоной плей-офф."
        )
        lines.append(
            "• Посмотри календарь: нет ли через 1–2 матча игры против прямого конкурента по таблице."
        )
        lines.append(
            "• Если такой матч есть, а текущий соперник — явный аутсайдер, "
            "риск недо-мотивации фаворита выше, чем обычно."
        )

    lines.extend(soft_spot_notes)

    lines.append("")
    lines.append(
        "Важно: эта логика не даёт готовый прогноз, но помогает понять, "
        "где фаворит может быть менее надёжен, чем подсказывает сухой коэффициент."
    )

    return "\n".join(lines)
