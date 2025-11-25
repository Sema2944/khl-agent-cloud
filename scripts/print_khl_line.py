import asyncio

from src.winline_client import get_khl_events_for_today


async def main() -> None:
    events = await get_khl_events_for_today()

    if not events:
        print("Матчей КХЛ не найдено (возможно, межсезонье или Winline ничего не даёт).")
        return

    for idx, e in enumerate(events, start=1):
        print(f"{idx}) {e.team1} — {e.team2} (id: {e.id})")
        if e.start_time:
            print("   Время начала:", e.start_time)

        # покажем максимум 3 маркета на матч
        for m in e.markets[:3]:
            print(f"   Маркет: {m.name}")
            for o in m.outcomes[:3]:
                print(f"      {o.name}: {o.price}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
