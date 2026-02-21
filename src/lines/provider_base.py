from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from .models import Match, Line


class LinesProvider(ABC):
    """
    Интерфейс источника линии.
    Хоть OddsAPI, хоть другой API — реализует эти методы.
    """

    @abstractmethod
    async def list_matches(
        self,
        sport: str,
        league: Optional[str] = None,
        date_iso: Optional[str] = None,
        query: Optional[str] = None,
    ) -> list[Match]:
        raise NotImplementedError

    @abstractmethod
    async def get_line(self, match_id: str) -> Line:
        raise NotImplementedError
