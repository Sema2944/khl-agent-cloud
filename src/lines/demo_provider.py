from __future__ import annotations

from datetime import datetime, timezone

from .models import Match, Market, Line
from .provider_base import LinesProvider


class DemoProvider(LinesProvider):
    async def list_matches(self, sport: str, league: str | None = None, date_iso: str | None = None, query: str | None = None):
        # Демка — чтобы MVP всегда отвечал
        return [
            Match(
                id="demo_khl_123456",
                sport="hockey",
                league="KHL",
                start_time_iso=None,
                home="СКА",
                away="ЦСКА",
            )
        ]

    async def get_line(self, match_id: str) -> Line:
        m = Match(
            id=match_id,
            sport="hockey",
            league="KHL",
            start_time_iso=None,
            home="СКА",
            away="ЦСКА",
        )

        markets = [
            Market(type="moneyline", home=1.85, draw=3.90, away=2.10),
            Market(type="total", value=5.5, over=1.87, under=1.95),
            Market(type="handicap", value=-1.5, home_handicap=2.35, away_handicap=1.60),
        ]

        return Line(
            match=m,
            markets=markets,
            source="demo",
            fetched_at_iso=datetime.now(timezone.utc).isoformat(),
        )
