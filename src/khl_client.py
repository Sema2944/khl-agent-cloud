# src/khl_client.py

from typing import List
from .parsing import Event, Market, Outcome


async def get_today_khl_events() -> List[Event]:
    """
    ВРЕМЕННАЯ ЗАГЛУШКА:
    вместо реального запроса в Winline возвращаем один тестовый матч.
    Это нужно, чтобы проверить, что /agent/query работает от начала до конца.
    """
    return [
        Event(
            id=123456,
            team1="СКА",
            team2="ЦСКА",
            league="KHL",
            sport="hockey",
            markets=[
                Market(
                    name="1X2",
                    outcomes=[
                        Outcome(name="1", price=1.85),
                        Outcome(name="X", price=3.90),
                        Outcome(name="2", price=2.10),
                    ],
                ),
            ],
        ),
    ]
