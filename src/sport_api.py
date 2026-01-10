# src/integrations/sport_api.py
import os
import httpx
from datetime import date
from typing import List, Optional

SPORT_API_BASE = os.getenv("SPORT_API_BASE", "https://api.api-sport.ru")
SPORT_API_KEY = os.getenv("SPORT_API_KEY")
SPORT_API_KEY_HEADER = os.getenv("SPORT_API_KEY_HEADER", "Authorization")
SPORT_API_KEY_PREFIX = os.getenv("SPORT_API_KEY_PREFIX", "")


class SportAPIError(Exception):
    pass


class MatchDTO:
    def __init__(self, raw: dict, sport_slug: str):
        self.id = raw.get("id")
        self.sport_slug = sport_slug

        home = raw.get("homeTeam", {}).get("translation", {}).get("ru") \
               or raw.get("homeTeam", {}).get("name", "Home")
        away = raw.get("awayTeam", {}).get("translation", {}).get("ru") \
               or raw.get("awayTeam", {}).get("name", "Away")

        self.title = f"{home} — {away}"

        self.league = (
            raw.get("tournament", {})
            .get("translations", {})
            .get("ru")
            or raw.get("tournament", {}).get("name", "")
        )

        self.status = raw.get("status")
        self.start_time = raw.get("dateEvent")


class OddsSnapshot:
    def __init__(self, raw:
