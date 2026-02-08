# src/parsing.py
# Финальная стабильная версия
# Формирование текстов PRE / LIVE / DAILY PRO

from datetime import datetime
from typing import List, Dict


def _safe(val, default="—"):
    return val if val not in (None, "", []) else default


# =========================
# DAILY PRO
# =========================
def build_daily_pro(matches: List[Dict]) -> str:
    today = datetime.now().strftime("%Y-%m-%d")

    header = (
        "🏒 DAILY PRO | ХОККЕЙ\n"
        f"📅 {today}\n\n"
        "🔥 Топ-события дня (для наблюдения)\n\n"
    )

    if not matches:
        return (
            header
            + "Сегодня нет подходящих матчей для обзора.\n\n"
            "ℹ️ Аналитика носит информационный характер."
        )

    blocks = []
    for i, m in enumerate(matches[:3], start=1):
        blocks.append(
            f"{i}) {_safe(m.get('team_a'))} — {_safe(m.get('team_b'))}\n"
            f"   🕒 {_safe(m.get('date'))}\n"
            "   Что смотреть:\n"
            "   • стартовые составы\n"
            "   • темп первых минут\n"
            "   • удаления и спецбригады\n"
        )

    footer = (
        "\n⛔ Риски\n"
        "• ротация составов\n"
        "• выставочный характер матча\n\n"
        "ℹ️ Материал является аналитическим и не является рекомендацией."
    )

    return header + "\n".join(blocks) + footer


# =========================
# PRE
# =========================
def build_pre(match: Dict) -> str:
    return (
        "🧠 PRE-обзор\n\n"
        f"Матч: {_safe(match.get('team_a'))} — {_safe(match.get('team_b'))}\n"
        f"Дата: {_safe(match.get('date'))}\n\n"
        "Что важно до старта:\n"
        "• составы и вратари\n"
        "• формат турнира\n"
        "• мотивация команд\n\n"
        "ℹ️ Обзор носит информационный характер."
    )


# =========================
# LIVE
# =========================
def build_live(match: Dict) -> str:
    return (
        "📊 LIVE-обзор\n\n"
        f"Матч: {_safe(match.get('team_a'))} — {_safe(match.get('team_b'))}\n"
        f"Счёт: {_safe(match.get('score'))}\n\n"
        "На что обратить внимание:\n"
        "• темп игры\n"
        "• удаления\n"
        "• давление в зоне\n\n"
        "ℹ️ Обзор носит информационный характер."
    )
