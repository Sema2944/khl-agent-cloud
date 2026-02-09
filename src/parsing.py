"""src/parsing.py

Rule-based text generator ("agent") used by the Telegram bot.

The bot imports this module dynamically and expects:

    async def run_dialog_agent(user_id: int, text: str) -> str

Supported commands (RU):
- ping
- матчи сегодня [ice-hockey|football]
- матч <match_id>
- ui match <match_id> <pre|live|livepro> <show|refresh>

Also supports internal Daily PRO prompt (contains "охотник дня"):
- Produces a Daily Pro bulletin for ICE-HOCKEY + FOOTBALL.

This file is deliberately self-contained and defensive to avoid crashes
on malformed API responses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


# --- Optional integration (present in the project) ---------------------------

try:
    from src.integrations.sport_api import SportAPIClient, SportAPIError  # type: ignore
except Exception:  # pragma: no cover
    SportAPIClient = None  # type: ignore

    class SportAPIError(Exception):
        pass


# --- Cache (in-memory; good enough for Render single instance) --------------

# Key: (sport, date_iso) -> dict(match_id -> match)
_MATCH_CACHE: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}


# --- Helpers ----------------------------------------------------------------

SUPPORTED_SPORTS = {
    "ice-hockey": "🏒 Хоккей",
    "football": "⚽ Футбол",
}


def _now_msk() -> datetime:
    if ZoneInfo is None:
        return datetime.now()
    try:
        return datetime.now(ZoneInfo("Europe/Moscow"))
    except Exception:
        return datetime.now()


def _today_iso() -> str:
    return _now_msk().date().isoformat()


def _safe_str(x: Any, default: str = "") -> str:
    if x is None:
        return default
    try:
        return str(x)
    except Exception:
        return default


def _get(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Get first present key from dict."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _truncate(s: str, n: int = 3800) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _normalize_match(sport: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize API match payload to a stable internal shape."""

    # IDs come in different shapes; keep as string.
    match_id = _safe_str(_get(raw, "id", "match_id", "matchId", default=""))

    country = _safe_str(
        _get(raw, "country", default=_get(raw, "country_name", "countryName", default=""))
    )

    league = _safe_str(
        _get(raw, "league", default=_get(raw, "league_name", "leagueName", default=""))
    )

    # Teams
    team_a = _safe_str(
        _get(raw, "team_a", "home", "home_team", "homeTeam", "teamHome", default="")
    )
    team_b = _safe_str(
        _get(raw, "team_b", "away", "away_team", "awayTeam", "teamAway", default="")
    )

    # Some APIs nest teams in 'teams'
    teams = raw.get("teams") if isinstance(raw.get("teams"), dict) else None
    if teams:
        team_a = team_a or _safe_str(_get(teams, "home", "a", "team_a", default=""))
        team_b = team_b or _safe_str(_get(teams, "away", "b", "team_b", default=""))

    # Date/time
    start_at = _safe_str(
        _get(raw, "date", "start_at", "startAt", "start_time", "startTime", default="")
    )

    # Score / status
    status = _safe_str(_get(raw, "status", "state", "match_status", default=""))

    score_home = _get(raw, "score_home", "home_score", "scoreHome", default=None)
    score_away = _get(raw, "score_away", "away_score", "scoreAway", default=None)

    # Some APIs nest score in 'score'
    score = raw.get("score") if isinstance(raw.get("score"), dict) else None
    if score:
        score_home = score_home if score_home is not None else _get(score, "home", "a")
        score_away = score_away if score_away is not None else _get(score, "away", "b")

    return {
        "id": match_id,
        "sport": sport,
        "country": country,
        "league": league,
        "team_a": team_a,
        "team_b": team_b,
        "start_at": start_at,
        "status": status,
        "score_home": score_home,
        "score_away": score_away,
        "raw": raw,
    }


def _fmt_score(m: Dict[str, Any]) -> str:
    sh = m.get("score_home")
    sa = m.get("score_away")
    if sh is None or sa is None:
        return ""
    return f"{sh}:{sa}"


def _fmt_dt(m: Dict[str, Any]) -> str:
    s = _safe_str(m.get("start_at", ""))
    if not s:
        return ""
    # Keep as-is; API may include date only or ISO datetime.
    return s


def _is_finished(status: str) -> bool:
    st = status.strip().lower()
    return any(k in st for k in ("finished", "ft", "ended", "final", "over", "заверш"))


def _is_live(status: str) -> bool:
    st = status.strip().lower()
    return any(k in st for k in ("live", "inplay", "in play", "1st", "2nd", "3rd", "ot", "pk"))


def _priority_bucket(sport: str, league: str) -> int:
    l = (league or "").lower()
    if sport == "ice-hockey":
        if "nhl" in l:
            return 0
        if "khl" in l or "кхл" in l:
            return 1
        if "ahl" in l:
            return 2
        if "mhl" in l or "мхл" in l:
            return 3
        return 9

    if sport == "football":
        # Top competitions first
        if "champions" in l or "лига чемпион" in l:
            return 0
        if "europa" in l or "лига европ" in l:
            return 1
        if "premier" in l and "league" in l:
            return 2
        if "la liga" in l or "primera" in l:
            return 3
        if "serie a" in l:
            return 4
        if "bundesliga" in l:
            return 5
        if "ligue 1" in l:
            return 6
        if "rpl" in l or "премьер-лига" in l or "росс" in l:
            return 7
        return 9

    return 9


async def _fetch_matches_today(sport: str) -> List[Dict[str, Any]]:
    if SportAPIClient is None:
        return []

    date_iso = _today_iso()
    cache_key = (sport, date_iso)
    cached = _MATCH_CACHE.get(cache_key)
    if cached:
        return list(cached.values())

    api = SportAPIClient()
    try:
        raw_list = await api.matches_by_date(sport=sport, date=date_iso)  # type: ignore
    except TypeError:
        # Older client signature (positional)
        raw_list = await api.matches_by_date(sport, date_iso)  # type: ignore
    except Exception as e:
        raise SportAPIError(str(e))

    matches: Dict[str, Dict[str, Any]] = {}
    if isinstance(raw_list, list):
        for r in raw_list:
            if isinstance(r, dict):
                m = _normalize_match(sport, r)
                if m["id"]:
                    matches[m["id"]] = m

    _MATCH_CACHE[cache_key] = matches
    return list(matches.values())


def _find_match(match_id: str) -> Optional[Dict[str, Any]]:
    match_id = match_id.strip()
    if not match_id:
        return None
    for (_, _), matches in _MATCH_CACHE.items():
        m = matches.get(match_id)
        if m:
            return m
    return None


def _format_match_card(m: Dict[str, Any]) -> str:
    title = f"{m.get('team_a','?')} — {m.get('team_b','?')}"
    league = m.get("league", "")
    country = m.get("country", "")
    dt = _fmt_dt(m)
    status = _safe_str(m.get("status", ""))
    score = _fmt_score(m)

    lines = [title]
    if league:
        lines.append(f"🏆 {league}")
    if country:
        lines.append(f"🌍 {country}")
    if dt:
        lines.append(f"🕒 {dt}")
    if status or score:
        if score:
            lines.append(f"📊 {score} ({status or 'status'})")
        else:
            lines.append(f"📊 {status}")
    lines.append("")
    lines.append("Кнопки: PRE / LIVE / LIVE PRO / Обновить LIVE")
    return "\n".join(lines).strip()


def _pre_text(m: Dict[str, Any]) -> str:
    title = f"🧠 PRE-обзор (коротко)\n{m.get('team_a','?')} — {m.get('team_b','?')}"
    lines = [title]
    league = m.get("league")
    if league:
        lines.append(f"🏆 {league}")
    dt = _fmt_dt(m)
    if dt:
        lines.append(f"🕒 {dt}")

    # Generic, sport-tailored checklist
    sport = m.get("sport")
    lines.append("")
    lines.append("Что проверить за 30–60 минут до начала:")
    lines.append("• составы / стартовые игроки")
    lines.append("• кто в воротах / кто в старте")
    lines.append("• новости: травмы, ротация, мотивация")

    if sport == "ice-hockey":
        lines.append("• спецбригады большинства/меньшинства (по ощущениям в 1-м периоде)")
        lines.append("• темп + удаления в начале (намёк на характер матча)")
    elif sport == "football":
        lines.append("• схема/роль лидеров, кто на стандартах")
        lines.append("• погодные условия и поле (если есть)" )

    lines.append("")
    lines.append("⚠️ Это справка, не рекомендация. Если данных мало — лучше наблюдать.")
    return "\n".join(lines).strip()


def _live_text(m: Dict[str, Any]) -> str:
    title = f"📡 LIVE-обзор\n{m.get('team_a','?')} — {m.get('team_b','?')}"
    lines = [title]
    score = _fmt_score(m)
    status = _safe_str(m.get("status", ""))
    if score:
        lines.append(f"📊 Счёт: {score}")
    if status:
        lines.append(f"⏱ Статус: {status}")

    lines.append("")
    if _is_finished(status):
        lines.append("Матч завершён. Можно разобрать итоги:")
        lines.append("• как менялся темп по периодам/таймам")
        lines.append("• ключевые моменты (удаления, голы, красные/травмы)")
    else:
        lines.append("Что смотреть прямо сейчас:")
        if m.get("sport") == "ice-hockey":
            lines.append("• темп и броски (высокий темп → больше моментов)")
            lines.append("• удаления подряд (часто ломают рисунок)")
            lines.append("• 5–7 минут после гола: команда реагирует или «плывёт»")
        else:
            lines.append("• давление: владение/опасные атаки (по ощущению просмотра)")
            lines.append("• стандарты и быстрые переходы")
            lines.append("• поведение после гола: садятся ли глубже")

    lines.append("")
    lines.append("⚠️ Это аналитическая заметка, без призывов к ставкам.")
    return "\n".join(lines).strip()


def _live_pro_text(m: Dict[str, Any]) -> str:
    title = f"🟢 LIVE PRO (структурно)\n{m.get('team_a','?')} — {m.get('team_b','?')}"
    lines = [title]
    score = _fmt_score(m)
    status = _safe_str(m.get("status", ""))
    if score:
        lines.append(f"📊 {score}")
    if status:
        lines.append(f"⏱ {status}")

    lines.append("")
    lines.append("Сценарии на матч (как читать игру):")
    if m.get("sport") == "ice-hockey":
        lines.append("1) Если много удалений → матч часто уходит в «качели» по моментам")
        lines.append("2) Если фаворит «висит» в зоне, но не забивает → следи за контратаками")
        lines.append("3) В конце периодов темп часто растёт — увеличивается риск ошибок")
    else:
        lines.append("1) Если одна команда высоко прессингует → следи за провалами за спиной")
        lines.append("2) Быстрый гол меняет план: у ведущей команды падает темп")
        lines.append("3) Стандарты решают много: угловые/штрафные — отдельный сюжет")

    lines.append("")
    lines.append("Риски / когда лучше просто наблюдать:")
    lines.append("• нет понимания составов/ролей — любой вывод будет слабым")
    lines.append("• линия/темп резко меняются без видимой причины")
    lines.append("• матч уже «закрыт» по рисунку (низкий темп, мало моментов)")

    lines.append("")
    lines.append("⚠️ Справка для понимания игры, не финансовый совет.")
    return "\n".join(lines).strip()


def _pick_top_events(matches: List[Dict[str, Any]], sport: str, n: int = 3) -> List[Dict[str, Any]]:
    # Prefer non-finished matches, then by league priority.
    def key(m: Dict[str, Any]) -> Tuple[int, int, str]:
        status = _safe_str(m.get("status", ""))
        finished = 1 if _is_finished(status) else 0
        pr = _priority_bucket(sport, _safe_str(m.get("league", "")))
        dt = _fmt_dt(m)
        return (finished, pr, dt)

    ms = sorted(matches, key=key)
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for m in ms:
        mid = _safe_str(m.get("id", ""))
        if not mid or mid in seen:
            continue
        out.append(m)
        seen.add(mid)
        if len(out) >= n:
            break
    return out


async def _daily_pro_combo_text() -> str:
    date_iso = _today_iso()

    sections: List[str] = []
    for sport in ("ice-hockey", "football"):
        try:
            matches = await _fetch_matches_today(sport)
        except Exception:
            matches = []

        top = _pick_top_events(matches, sport, n=3)

        header = f"{SUPPORTED_SPORTS.get(sport, sport)} | DAILY PRO\n📅 {date_iso}"
        lines = [header, "", "🔥 Топ-3 события дня (для наблюдения)", ""]

        if not top:
            lines.append("Нет данных по матчам на сегодня (или API временно недоступен).")
        else:
            for i, m in enumerate(top, start=1):
                title = f"{m.get('team_a','?')} — {m.get('team_b','?')}"
                league = _safe_str(m.get("league", ""))
                dt = _fmt_dt(m)
                lines.append(f"{i}) {title}")
                if league:
                    lines.append(f"   🏆 {league}")
                if dt:
                    lines.append(f"   🕒 {dt}")
                lines.append("   Что смотреть:")
                lines.append("   • составы/стартовые (за 30–60 мин)")
                lines.append("   • темп в начале и дисциплина")
                lines.append("   • как команда реагирует после пропущенного")
                lines.append("")

        lines.append("🎯 Экспресс-конструктор (без навязывания)")
        lines.append("• Выбери 2 события из топ-3 и собери аккуратный «план просмотра».")
        lines.append("• Если нет подтверждений по составам — лучше просто наблюдать.")
        lines.append("")
        lines.append("⛔ Риски / когда пропустить")
        lines.append("• неожиданная ротация / резервный состав")
        lines.append("• резкая смена рисунка без видимых причин")
        lines.append("• мало данных — выводы будут шумными")

        sections.append("\n".join(lines).strip())

    final = "\n\n".join(sections)
    return _truncate(final)


def _help_text() -> str:
    return (
        "Не понял команду.\n\n"
        "Доступно:\n"
        "• ping\n"
        "• матчи сегодня [ice-hockey|football]\n"
        "• матч <match_id>\n"
        "• ui match <match_id> <pre|live|livepro> <show|refresh>\n"
    ).strip()


# --- Public entrypoint ------------------------------------------------------

async def run_dialog_agent(user_id: int, text: str) -> str:
    """Main router called by the bot.

    Parameters
    ----------
    user_id: telegram user id
    text: user's message (or internal command)
    """

    t = (text or "").strip()
    t_low = t.lower()

    # Internal Daily PRO prompt from /jobs/daily-pro
    if "охотник дня" in t_low or "daily pro" in t_low:
        return await _daily_pro_combo_text()

    if t_low == "ping":
        return "pong"

    # Matches today
    m = re.match(r"^матчи\s+сегодня(?:\s+([a-z\-]+))?$", t_low)
    if m:
        sport = (m.group(1) or "").strip() or "ice-hockey"
        if sport not in SUPPORTED_SPORTS:
            return "Выбери спорт: ice-hockey или football"

        try:
            matches = await _fetch_matches_today(sport)
        except Exception:
            return "⚠️ Не удалось получить матчи (API временно недоступен)."

        # Compact list for CLI usage; UI navigation is handled in app.py
        top = _pick_top_events(matches, sport, n=15)
        lines = [f"{SUPPORTED_SPORTS[sport]} | Матчи сегодня ({_today_iso()})", ""]
        if not top:
            lines.append("Нет матчей или нет данных.")
            return "\n".join(lines)

        for i, mm in enumerate(top, start=1):
            title = f"{mm.get('team_a','?')} — {mm.get('team_b','?')}"
            league = _safe_str(mm.get("league", ""))
            dt = _fmt_dt(mm)
            mid = _safe_str(mm.get("id", ""))
            piece = f"{i}) {title}"
            if league:
                piece += f" | {league}"
            if dt:
                piece += f" | {dt}"
            if mid:
                piece += f" | id={mid}"
            lines.append(piece)
        lines.append("\nЧтобы открыть карточку: матч <match_id>")
        return _truncate("\n".join(lines))

    # Single match card
    m2 = re.match(r"^матч\s+(\d+)$", t_low)
    if m2:
        match_id = m2.group(1)
        mm = _find_match(match_id)
        if not mm:
            return "Матч не найден в кеше. Сначала: матчи сегодня ice-hockey или matчи сегодня football"
        return _truncate(_format_match_card(mm))

    # UI command from callback router
    m3 = re.match(r"^ui\s+match\s+(\d+)\s+(pre|live|livepro)\s+(show|refresh)$", t_low)
    if m3:
        match_id, mode, _action = m3.group(1), m3.group(2), m3.group(3)
        mm = _find_match(match_id)
        if not mm:
            return "Матч не найден в кеше. Открой его через «Матчи сегодня» ещё раз."

        if mode == "pre":
            return _truncate(_pre_text(mm))
        if mode == "live":
            return _truncate(_live_text(mm))
        return _truncate(_live_pro_text(mm))

    return _help_text()
