#!/usr/bin/env python3
"""
Diagnostic script: check basketball API season formats per league.

Tests whether each league works with "YYYY-YYYY" (string) or YYYY (integer).
Usage: python scripts/check_basketball_leagues.py

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
BASE = "https://v1.basketball.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

# Leagues to test
LEAGUES = {
    12:  "NBA",
    120: "Euroleague",
    117: "Eurocup",
    179: "VTB League",
}

# Season formats to test for each league
TEST_SEASONS = ["2025-2026", "2024-2025", 2025, 2024, 2026]


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
        print("1. AVAILABLE SEASONS (basketball)")
        print("=" * 60)
        resp = await client.get(f"{BASE}/seasons", headers=HEADERS)
        data = resp.json()
        seasons = data.get("response", [])
        errors = data.get("errors")
        if errors:
            print(f"  ERRORS: {errors}")
        else:
            print(f"  Seasons ({len(seasons)}):")
            for s in sorted(seasons, reverse=True)[:15]:
                print(f"    {s!r}  (type={type(s).__name__})")

        # 2) Check leagues info
        print()
        print("=" * 60)
        print("2. LEAGUE INFO")
        print("=" * 60)
        for league_id, league_name in LEAGUES.items():
            resp = await client.get(
                f"{BASE}/leagues",
                headers=HEADERS,
                params={"id": league_id},
            )
            data = resp.json()
            items = data.get("response", [])
            errors = data.get("errors")
            if errors:
                print(f"  League {league_id} ({league_name}): ERRORS={errors}")
                continue
            if not items:
                print(f"  League {league_id} ({league_name}): NOT FOUND")
                continue
            lg = items[0]
            name = lg.get("name", "?")
            country = lg.get("country", {})
            cname = country.get("name", "") if isinstance(country, dict) else str(country)
            seasons_list = lg.get("seasons", [])
            latest = sorted(
                [s.get("season") for s in seasons_list if s.get("season")],
                reverse=True,
            )[:5]
            print(f"  ID={league_id:4d}  {name:30s}  {cname:15s}  latest_seasons={latest}")

        # 3) Test each league with each season format
        print()
        print("=" * 60)
        print("3. TEST SEASON FORMATS PER LEAGUE")
        print("=" * 60)

        results = {}  # {league_id: {season: (count, error)}}
        for league_id, league_name in LEAGUES.items():
            print(f"\n  League {league_id} ({league_name}):")
            results[league_id] = {}

            for season in TEST_SEASONS:
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

                if errors:
                    status = f"ERROR: {errors}"
                    results[league_id][season] = (0, errors)
                else:
                    status = f"{len(items):4d} games"
                    results[league_id][season] = (len(items), None)

                season_repr = f"{season!r}"
                print(f"    season={season_repr:16s} → {status}  (remaining: {remaining})")

                if items and len(items) > 0:
                    g = items[0]
                    teams = g.get("teams", {})
                    h = teams.get("home", {}).get("name", "?")
                    a = teams.get("away", {}).get("name", "?")
                    d = g.get("date", "?")
                    print(f"      sample: {h} vs {a}  ({d})")

        # 4) Summary
        print()
        print("=" * 60)
        print("4. SUMMARY — RECOMMENDED SEASON FORMAT")
        print("=" * 60)
        for league_id, league_name in LEAGUES.items():
            league_results = results.get(league_id, {})
            best_season = None
            best_count = 0
            for season, (count, err) in league_results.items():
                if not err and count > best_count:
                    best_count = count
                    best_season = season

            if best_season is not None:
                fmt = "STRING" if isinstance(best_season, str) else "INTEGER"
                print(f"  {league_name:20s} (ID={league_id:3d}): season={best_season!r} ({fmt}) → {best_count} games")
            else:
                print(f"  {league_name:20s} (ID={league_id:3d}): NO WORKING SEASON FOUND")

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
