# bot.py
import asyncio
from datetime import date

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

from api_sport_client import ApiSportClient

# 🔹 1) ВСТАВЬ ТОКЕН ОТ BotFather
TELEGRAM_BOT_TOKEN = "ТОКЕН_ТВОЕГО_БТА"

# 🔹 2) ВСТАВЬ API-ключ из https://app.api-sport.ru/dashboard
API_SPORT_KEY = "ТВОЙ_API_SPORT_KEY"

# 🔹 3) ВСТАВЬ ID турнира КХЛ (я помогу найти)
KHL_TOURNAMENT_ID = 1234  # ← заменим позже


bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
api_client = ApiSportClient(API_SPORT_KEY)


def format_matches(matches: list[dict]) -> str:
    if not matches:
        return "Матчей не найдено."

    lines = []
    for m in matches:
        home = (
            m.get("homeTeam", {}).get("name")
            or m.get("home_team")
            or "Home"
        )
        away = (
            m.get("awayTeam", {}).get("name")
            or m.get("away_team")
            or "Away"
        )

        start = m.get("startTime") or m.get("start_time", "")
        status = m.get("status", "")

        score_home = m.get("homeScore") or m.get("home_score")
        score_away = m.get("awayScore") or m.get("away_score")

        if score_home is not None and score_away is not None:
            score = f"{score_home}:{score_away}"
        else:
            score = "–:–"

        lines.append(f"{start} | {home} vs {away} | {score} ({status})")

    return "\n".join(lines)


@dp.message(Command("khl_today"))
async def khl_today(message: types.Message):
    try:
        data = api_client.get_khl_matches_for_date(KHL_TOURNAMENT_ID, date.today())
        matches = data.get("data") or data.get("matches") or data
        await message.answer(format_matches(matches))
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@dp.message(Command("khl_live"))
async def khl_live(message: types.Message):
    try:
        data = api_client.get_khl_live_matches(KHL_TOURNAMENT_ID)
        matches = data.get("data") or data.get("matches") or data
        await message.answer(format_matches(matches))
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
