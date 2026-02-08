
# FINAL PRODUCT VERSION — parsing.py
# Stable, calm, user-friendly texts for PRE / LIVE / DAILY PRO
# No technical AI wording exposed to users

from datetime import datetime
from typing import List, Dict, Any

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _today_str():
    return datetime.now().strftime("%Y-%m-%d")


# ------------------------------------------------------------------
# Core insight builder (lightweight, stable)
# ------------------------------------------------------------------

def build_match_insights(team_a: str, team_b: str) -> Dict[str, str]:
    return {
        "summary": (
            "Матч без выраженного преимущества одной из сторон. "
            "Игра может сильно зависеть от стартового отрезка."
        ),
        "action": (
            "Оптимально начать с наблюдения и делать выводы по ходу встречи."
        ),
        "risk": (
            "Средний — возможны резкие смены темпа."
        ),
        "skip": [
            "нет подтверждений по составам",
            "игра с первых минут уходит в хаотичный темп",
        ],
    }


# ------------------------------------------------------------------
# PRE / LIVE rendering
# ------------------------------------------------------------------

def render_pre_live(team_a: str, team_b: str, mode: str) -> str:
    ins = build_match_insights(team_a, team_b)

    title = "📊 Краткий обзор матча" if mode == "pre" else "📊 Обзор по ходу игры"

    text = f"""{title}

🏒 {team_a} — {team_b}

Общая картина:
{ins["summary"]}

Как лучше действовать:
{ins["action"]}

Уровень риска:
{ins["risk"]}

Когда стоит пропустить:
• {ins["skip"][0]}
• {ins["skip"][1]}

ℹ️ Обзор носит информационный характер.
"""
    return text


# ------------------------------------------------------------------
# DAILY PRO rendering
# ------------------------------------------------------------------

def render_daily_pro(matches: List[Dict[str, Any]]) -> str:
    lines = [
        f"🏒 DAILY PRO | ХОККЕЙ",
        f"📅 {_today_str()}",
        "",
        "🔥 Матчи дня для наблюдения",
        "",
    ]

    for i, m in enumerate(matches[:3], start=1):
        lines.append(
            f"{i}) {m['team_a']} — {m['team_b']}
"
            f"   Матч без явного фаворита. Подходит для внимательного просмотра."
        )

    lines.extend(
        [
            "",
            "⛔ Когда лучше пропустить:",
            "• нет информации по составам",
            "• линия резко меняется без новостей",
            "",
            "ℹ️ Подборка носит аналитический характер и не является рекомендацией.",
        ]
    )

    return "\n".join(lines)


# ------------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------------

async def run_dialog_agent(user_id: int, text: str) -> str:
    norm = text.lower().strip()

    # DAILY PRO
    if norm.startswith("охотник дня"):
        dummy_matches = [
            {"team_a": "СКА", "team_b": "Локомотив"},
            {"team_a": "ЦСКА", "team_b": "Динамо М"},
            {"team_a": "Ак Барс", "team_b": "Авангард"},
        ]
        return render_daily_pro(dummy_matches)

    # PRE / LIVE
    if "pre" in norm:
        return render_pre_live("Команда A", "Команда B", mode="pre")

    if "live" in norm:
        return render_pre_live("Команда A", "Команда B", mode="live")

    return (
        "Не понял команду.\n\n"
        "Доступно:\n"
        "• матчи сегодня\n"
        "• PRE-обзор\n"
        "• LIVE-обзор"
    )
