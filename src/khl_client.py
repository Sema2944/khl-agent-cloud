# src/khl_client.py

from typing import List
from .parsing import Event, get_khl_events_for_today


async def get_today_khl_events() -> List[Event]:
    """
    Боевая версия:
    реально тянем события КХЛ через парсер Winline.
    В случае ошибок их перехватит run_agent в service.py.
    """
    return await get_khl_events_for_today()
