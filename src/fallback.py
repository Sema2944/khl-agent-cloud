# src/fallback.py
from __future__ import annotations
from typing import Dict, Any


def fallback_analysis(reason: str, mode: str, snapshot: Dict[str, Any]) -> str:
    match_line = f"{snapshot.get('home_team','')} — {snapshot.get('away_team','')}".strip(" —")
    league_line = f"{snapshot.get('league','')} • {snapshot.get('country','')}".strip(" •")
    score = snapshot.get("score", "")
    status = snapshot.get("status_raw", "")

    lines = []
    if mode.startswith("live"):
        lines.append("🟢 LIVE-авторазбор")
        lines.append("Быстрый системный анализ по доступным данным.\n")
        lines.append(f"Матч: {match_line}")
        if league_line:
            lines.append(f"Лига: {league_line}")
        if status:
            lines.append(f"Статус: {human_status(status)}")
        if score:
            lines.append(f"Счёт: {score}\n")

        lines.append("Что проверить прямо сейчас:")
        lines.append("• темп (частота моментов/бросков)")
        lines.append("• удаления/штрафное время (если хоккей)")
        lines.append("• кто давит последние 5–10 минут")
        lines.append("• резкие изменения линии без новостей\n")

        lines.append("Риски:")
        lines.append("• мало данных — не увеличивай сумму")
        lines.append("• если не уверен в картине игры — лучше пропустить")

    else:
        lines.append("📌 PRE-авторазбор")
        lines.append("Короткий чек-лист перед матчем.\n")
        lines.append(f"Матч: {match_line}")
        if league_line:
            lines.append(f"Лига: {league_line}\n")

        lines.append("Что проверить за 30–60 минут до начала:")
        lines.append("• составы / травмы / вратари (хоккей — критично)")
        lines.append("• мотивация: серия, турнирная ситуация, дерби")
        lines.append("• движение линии и причины (если они известны)\n")

        lines.append("Риски:")
        lines.append("• резкое падение коэффициентов без новостей — лучше пропустить")
        lines.append("• если данных мало — не усложняй решения")

    lines.append("\nℹ️ Аналитика, не рекомендация.")
    return "\n".join(lines)


def human_status(s: str) -> str:
    s = (s or "").upper()
    mapping = {
        "INPROGRESS": "Идёт",
        "LIVE": "Идёт",
        "FINISHED": "Завершён",
        "FT": "Завершён",
        "NS": "Не начался",
        "SCHEDULED": "Запланирован",
    }
    return mapping.get(s, s)
