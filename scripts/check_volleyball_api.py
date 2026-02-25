#!/usr/bin/env python3
"""
Diagnostic script: check volleyball API seasons and leagues.
Usage: python scripts/check_volleyball_api.py

Requires API_SPORTS_KEY in .env or environment.
"""
import asyncio
import os
import sys

# Load .env if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_KEY = os.getenv("API_SPORTS_KEY", "")
BASE = "https://v1.volleyball.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}


async def main():
    if not API_KEY:
        print("ERROR: API_SPORTS_KEY not set. Put it in .env or export it.")
        sys.exit(1)

    try:
        import httpx
    except ImportError:
        print("ERROR: httpx not installed. Run: pip install httpx")
        sys.exit(1)

    async with httpx.AsyncClient(timeout=30) as client:
        # 1) Available seasons
        print("=" * 60)
        print("1. AVAILABLE SEASONS")
        print("=" * 60)
        resp = await client.get(f"{BASE}/seasons", headers=HEADERS)
        data = resp.json()
        seasons = data.get("response", [])
        errors = data.get("errors")
        if errors:
            print(f"  ERRORS: {errors}")
        else:
            print(f"  Seasons ({len(seasons)}):")
            for s in sorted(seasons, reverse=True):
                print(f"    {s}")

        # 2) Available leagues
        print()
        print("=" * 60)
        print("2. AVAILABLE LEAGUES")
        print("=" * 60)
        resp = await client.get(f"{BASE}/leagues", headers=HEADERS)
        data = resp.json()
        leagues = data.get("response", [])
        errors = data.get("errors")
        if errors:
            print(f"  ERRORS: {errors}")
        else:
            print(f"  Leagues ({len(leagues)}):")
            # Show leagues with their IDs, filter for key ones
            target_keywords = ["суперлига", "superliga", "russia", "cev", "champions",
                               "serie a", "italy", "italia", "россия"]
            highlighted = []
            for lg in leagues:
                lid = lg.get("id")
                name = lg.get("name", "")
                country = lg.get("country", {})
                cname = country.get("name", "") if isinstance(country, dict) else str(country)
                seasons_list = lg.get("seasons", [])
                latest_seasons = sorted(
                    [s.get("season") for s in seasons_list if s.get("season")],
                    reverse=True,
                )[:3]

                line = f"    ID={lid:4d}  {name:40s}  {cname:20s}  seasons={latest_seasons}"

                # Check if this is one of our target leagues
                search_str = f"{name} {cname}".lower()
                if any(kw in search_str for kw in target_keywords):
                    highlighted.append(f" >> {line}")
                else:
                    # Just print all for reference
                    pass

            # Print highlighted first
            if highlighted:
                print("\n  === TARGET LEAGUES (Russia, CEV, Serie A) ===")
                for h in highlighted:
                    print(h)
                print()

            # Print all leagues
            print(f"\n  === ALL LEAGUES ({len(leagues)}) ===")
            for lg in leagues:
                lid = lg.get("id")
                name = lg.get("name", "")
                country = lg.get("country", {})
                cname = country.get("name", "") if isinstance(country, dict) else str(country)
                seasons_list = lg.get("seasons", [])
                latest_seasons = sorted(
                    [s.get("season") for s in seasons_list if s.get("season")],
                    reverse=True,
                )[:3]
                print(f"    ID={lid:4d}  {name:40s}  {cname:20s}  seasons={latest_seasons}")

        # 3) Test specific leagues with different season formats
        print()
        print("=" * 60)
        print("3. TEST LEAGUE QUERIES")
        print("=" * 60)
        test_cases = [
            (78, "Суперлига"),
            (37, "CEV Champions League"),
            (30, "Serie A1"),
        ]
        test_seasons = ["2025-2026", "2024-2025", "2025", "2024"]

        for league_id, league_name in test_cases:
            print(f"\n  League {league_id} ({league_name}):")
            for season in test_seasons:
                params = {
                    "league": league_id,
                    "season": season,
                    "timezone": "Europe/Moscow",
                }
                resp = await client.get(f"{BASE}/games", headers=HEADERS, params=params)
                remaining = resp.headers.get("x-ratelimit-requests-remaining", "?")
                data = resp.json()
                items = data.get("response", [])
                errors = data.get("errors")
                status = "ERROR" if errors else f"{len(items)} games"
                print(f"    season={season:12s} → {status}  (remaining: {remaining})")
                if errors:
                    print(f"      errors: {errors}")
                if items and len(items) > 0:
                    # Show first game as sample
                    g = items[0]
                    teams = g.get("teams", {})
                    h = teams.get("home", {}).get("name", "?")
                    a = teams.get("away", {}).get("name", "?")
                    print(f"      sample: {h} vs {a}")

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
