# src/news_rss.py
"""
RSS news parser for sports teams.
Fetches latest news from Russian sports RSS feeds.

Uses feedparser (pip install feedparser).
Falls back gracefully if feedparser is not installed.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# RSS feeds by sport category
RSS_FEEDS: Dict[str, List[str]] = {
    "ice-hockey": [
        "https://www.championat.com/hockey/_khl/rss.xml",
        "https://sport-express.ru/hockey/khl/rss/",
    ],
    "hockey": [
        "https://www.championat.com/hockey/_khl/rss.xml",
        "https://sport-express.ru/hockey/khl/rss/",
    ],
    "football": [
        "https://www.championat.com/football/rss.xml",
        "https://sport-express.ru/football/rss/",
    ],
    "basketball": [
        "https://www.championat.com/basketball/rss.xml",
    ],
}

# Team name aliases for fuzzy matching in news
TEAM_ALIASES: Dict[str, List[str]] = {
    # KHL teams
    "сибирь": ["сибирь", "сибирью", "сибири"],
    "амур": ["амур", "амуром", "амура", "хабаровск"],
    "ска": ["ска", "скa"],
    "цска": ["цска", "цскa"],
    "авангард": ["авангард", "авангарду", "аванагарда", "омск"],
    "металлург": ["металлург", "магнитка", "магнитогорск"],
    "ак барс": ["ак барс", "казань"],
    "салават юлаев": ["салават", "салавату", "юлаев", "уфа"],
    "спартак": ["спартак"],
    "динамо": ["динамо"],
    "локомотив": ["локомотив", "ярославль"],
    "трактор": ["трактор", "челябинск"],
    "торпедо": ["торпедо", "нижний"],
    "автомобилист": ["автомобилист", "екатеринбург"],
    "барыс": ["барыс", "астана"],
    "адмирал": ["адмирал", "владивосток"],
    "витязь": ["витязь"],
    "нефтехимик": ["нефтехимик", "нижнекамск"],
    "северсталь": ["северсталь", "череповец"],
    "куньлунь": ["куньлунь", "ред стар", "red star"],
    "сочи": ["сочи"],
    "лада": ["лада", "тольятти"],
}


def _get_team_keywords(team_name: str) -> List[str]:
    """Get search keywords for a team name."""
    name_lower = team_name.lower().strip()
    # Check aliases
    for base, aliases in TEAM_ALIASES.items():
        if name_lower in aliases or base in name_lower:
            return aliases
        for alias in aliases:
            if alias in name_lower:
                return aliases
    # Fallback: use the name itself and its parts
    parts = [name_lower]
    words = name_lower.split()
    if len(words) > 1:
        parts.extend(w for w in words if len(w) >= 3)
    return parts


async def get_team_news(
    team_name: str,
    sport_slug: str = "ice-hockey",
    limit: int = 3,
) -> List[Dict[str, str]]:
    """
    Fetch recent news about a team from RSS feeds.
    Returns list of {title, summary, published, link}.
    Never raises.
    """
    try:
        import feedparser
    except ImportError:
        logger.debug("feedparser not installed, skipping news")
        return []

    feeds = RSS_FEEDS.get(sport_slug, [])
    if not feeds:
        return []

    keywords = _get_team_keywords(team_name)
    results: List[Dict[str, str]] = []

    for feed_url in feeds:
        try:
            # Run feedparser in thread pool (it's synchronous + network I/O)
            feed = await asyncio.to_thread(feedparser.parse, feed_url)

            for entry in (feed.entries or []):
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                text = (title + " " + summary).lower()

                if any(kw in text for kw in keywords):
                    results.append({
                        "title": title,
                        "summary": _clean_summary(summary),
                        "published": entry.get("published", ""),
                        "link": entry.get("link", ""),
                    })

                    if len(results) >= limit:
                        return results

        except Exception:
            logger.debug("RSS fetch failed for %s", feed_url)
            continue

    return results[:limit]


def _clean_summary(text: str) -> str:
    """Remove HTML tags from summary."""
    import re
    clean = re.sub(r"<[^>]+>", "", text)
    clean = clean.replace("&nbsp;", " ").replace("&amp;", "&")
    return clean[:300].strip()
