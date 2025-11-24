# scripts/print_khl_line.py

import asyncio

from src.winline_client import get_khl_events_for_today


async def main():
    events = await get_khl_events_for_today()
    if not events:
        print("КХЛ в линии не найдено.")
        return

    for e in events:
        print(f"{e.id}: {e.team1} — {e.team2} ({e.league})")
        if e.markets:
            m = e.markets[0]
            print(f"  Маркет: {m.name}")
            for o in m.outcomes:
                print(f"    {o.name}: {o.price}")
        print("-" * 40)


if __name__ == "__main__":
    asyncio.run(main())
