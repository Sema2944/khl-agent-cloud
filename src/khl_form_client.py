# src/khl_form_client.py

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List

import httpx

logger = logging.getLogger(__name__)


@dataclass
class TeamForm:
    """
    Упрощённое описание формы команды по последним матчам.
    """
    team_name: str
    games: int
    wins: int
    losses: int
    goals_for: float
    goals_against: float
    avg_total: float


# ----------------------------------------------------------------------
# Вспомогательные функции нормализации
# ----------------------------------------------------------------------


def _norm_team_name(name: str) -> str:
    """
    Нормализуем имя команды:
    - в верхний регистр
    - убираем лишние пробелы
    - приводим Ё → Е
    """
    if not name:
        return ""
    name = name.replace("Ё", "Е").replace("ё", "е")
    name = re.sub(r"\s+", " ", name).strip()
    return name.upper()


def _is_same_team(target: str, candidate: str) -> bool:
    """
    Грубое сравнение названий команд:
    - нормализуем
    - проверяем совпадение или включение (на случай 'ДИНАМО М' vs 'ДИНАМО МОСКВА')
    """
    t = _norm_team_name(target)
    c = _norm_team_name(candidate)
    if not t or not c:
        return False

    if t == c:
        return True

    # На всякий случай — короткое/длинное имя
    return t in c or c in t


# ----------------------------------------------------------------------
# Основная логика: парсим календарь КХЛ на Championat
# ----------------------------------------------------------------------

CHAMPIONAT_KHL_CAL_URL = (
    "https://www.championat.com/hockey/_superleague/tournament/6608/calendar/"
)
# 6608 — id текущего сезона КХЛ 2025/26 на Championat.
# Если лига поменяет id сезона, это место придётся обновить руками.


async def _fetch_calendar_html() -> str | None:
    """
    Тянем HTML календаря КХЛ с Championat.

    Если не получилось (таймаут / 5xx / блокировка) — вернём None,
    а наверху аккуратно отработаем заглушку.
    """
    headers = {
        # Чуть более "человеческий" User-Agent, чтобы не пугать сайт.
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        )
    }

    try:
        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            resp = await client.get(CHAMPIONAT_KHL_CAL_URL)
            if resp.status_code != 200:
                logger.warning(
                    "Championat calendar returned status %s", resp.status_code
                )
                return None
            return resp.text
    except Exception:
        logger.exception("Failed to fetch Championat KHL calendar")
        return None


def _parse_matches_for_team(
    html: str,
    team_name: str,
    max_games: int = 10,
) -> List[tuple[datetime, int, int]]:
    """
    Из общей HTML-страницы календаря вытаскиваем матчи нужной команды.

    Возвращаем список кортежей:
    (дата, забитые, пропущенные) — только по завершённым матчам.
    """
    # Убираем теги, оставляем голый текст — так проще регуляркой.
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text)

    # Регулярка по мотивам структуры страницы:
    # дата, время, команда1, команда2, счёт 4 : 3 (может быть с "Б" или "ОТ" после счёта)
    pattern = re.compile(
        r"(?P<date>\d{2}\.\d{2}\.\d{4})\s+\d{2}:\d{2}\s+"
        r"(?P<team1>[A-Za-zА-Яа-яЁё\"«»\-\s]+?)\s+–\s+"
        r"(?P<team2>[A-Za-zА-Яа-яЁё\"«»\-\s]+?)\s+"
        r"(?P<g1>\d+)\s*:\s*(?P<g2>\d+)",
        re.DOTALL,
    )

    target_matches: List[tuple[datetime, int, int]] = []

    for m in pattern.finditer(text):
        date_str = m.group("date")
        team1 = m.group("team1").strip()
        team2 = m.group("team2").strip()
        g1 = int(m.group("g1"))
        g2 = int(m.group("g2"))

        try:
            dt = datetime.strptime(date_str, "%d.%m.%Y")
        except ValueError:
            continue

        is_team1 = _is_same_team(team_name, team1)
        is_team2 = _is_same_team(team_name, team2)

        if not (is_team1 or is_team2):
            continue

        if is_team1 and not is_team2:
            gf, ga = g1, g2
        elif is_team2 and not is_team1:
            gf, ga = g2, g1
        else:
            # Если вдруг совпало с обеими (какой-то странный случай) — пропускаем.
            continue

        target_matches.append((dt, gf, ga))

    # Сортируем по дате (новые сверху)
    target_matches.sort(key=lambda x: x[0], reverse=True)

    if max_games > 0:
        target_matches = target_matches[:max_games]

    return target_matches


async def get_team_form(team_name: str, max_games: int = 10) -> Optional[TeamForm]:
    """
    Основная точка входа:

    - тянем календарь КХЛ с Championat;
    - вытаскиваем матчи нужной команды;
    - считаем форму по последним N играм (по умолчанию 10).

    Если не получилось спарсить или матчей ещё нет — вернём None,
    а наверху (в run_agent) уже есть обработка этого кейса.
    """
    if not team_name:
        return None

    html = await _fetch_calendar_html()
    if not html:
        logger.warning("No HTML for KHL calendar; returning None form for %s", team_name)
        return None

    matches = _parse_matches_for_team(html, team_name, max_games=max_games)
    if not matches:
        logger.info("No matches found in calendar for team '%s'", team_name)
        return None

    games = len(matches)
    total_gf = sum(gf for _, gf, _ in matches)
    total_ga = sum(ga for _, _, ga in matches)
    wins = sum(1 for _, gf, ga in matches if gf > ga)
    losses = sum(1 for _, gf, ga in matches if gf < ga)

    avg_gf = total_gf / games if games > 0 else 0.0
    avg_ga = total_ga / games if games > 0 else 0.0
    avg_total = (total_gf + total_ga) / games if games > 0 else 0.0

    return TeamForm(
        team_name=team_name,
        games=games,
        wins=wins,
        losses=losses,
        goals_for=avg_gf,
        goals_against=avg_ga,
        avg_total=avg_total,
    )
