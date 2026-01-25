# src/ui_text.py
from __future__ import annotations

# ВАЖНО:
# telegram_bot/app.py импортирует MAIN_MENU_TEXT.
# Держим здесь все короткие UI-строки, чтобы не плодить дубли.

MAIN_MENU_TEXT = (
    "Главное меню\n\n"
    "🏟 Матчи сегодня\n"
    "🧠 AI Аналитика\n"
    "⭐ Premium\n"
    "📊 Профиль"
)

CHOOSE_SPORT_TEXT = "🏟 Выбери спорт:"
CHOOSE_COUNTRY_TEXT = "Выбери страну:"
CHOOSE_LEAGUE_TEXT = "Выбери лигу:"
CHOOSE_MATCH_TEXT = "Выбери матч:"


def fmt_header_matches_today(title: str, day_iso: str) -> str:
    return f"🏟 Матчи сегодня (по МСК) — {title}\nДата: {day_iso}\n"


def fmt_country_header(country: str) -> str:
    return f"🏳️ Страна: {country}\n\n{CHOOSE_LEAGUE_TEXT}"


def fmt_league_header(country: str, league: str) -> str:
    return f"🏳️ {country}\n🏆 Лига: {league}\n\n{CHOOSE_MATCH_TEXT}"


def fmt_match_header(title: str, league: str = "", country: str = "") -> str:
    lines = [f"🏒 {title}"]
    if country:
        lines.append(f"🏳️ {country}")
    if league:
        lines.append(f"🏆 {league}")
    return "\n".join(lines)
