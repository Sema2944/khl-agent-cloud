"""src.parsing

Устойчивый генератор ответов для кнопок PRE / LIVE / DAILY PRO.

Принципы:
- Без обещаний/гарантий.
- Без агрессивных слов "ставь/бери/железо/пушка/100%".
- Максимально стабильный: любой входной текст не должен ломать модуль.

Интеграция:
- src.telegram_bot.app импортирует модуль как "src.parsing" и вызывает
  async def run_dialog_agent(user_id: int, text: str) -> str

Примечание:
Этот модуль не знает, как именно вы получаете матчи/статы.
Он умеет "вытащить" из входного текста команды/контекст и собрать
человеческий, структурированный ответ.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple


# -----------------------------
# Safety helpers
# -----------------------------

def _safe_str(s: object) -> str:
    try:
        return str(s)
    except Exception:
        return ""


def _truncate(text: str, limit: int = 3800) -> str:
    text = _safe_str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# -----------------------------
# Domain parsing
# -----------------------------

SPORT_ALIASES = {
    "ice-hockey": ["ice-hockey", "хоккей", "кхл", "nhl", "нхл"],
    "football": ["football", "футбол", "uefa", "fifa", "epl", "premier league", "la liga"],
}


@dataclass
class MatchLine:
    sport: Optional[str]
    a: str
    b: str
    league: Optional[str] = None
    country: Optional[str] = None
    when: Optional[str] = None
    match_id: Optional[str] = None
    status: Optional[str] = None
    score: Optional[str] = None


def _guess_sport(text: str) -> Optional[str]:
    low = text.lower()
    for sport, keys in SPORT_ALIASES.items():
        for k in keys:
            if k in low:
                return sport
    return None


def _extract_date(text: str) -> Optional[str]:
    # 2026-02-09
    m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    if m:
        return m.group(1)
    # 09.02.2026
    m = re.search(r"\b(\d{2})\.(\d{2})\.(20\d{2})\b", text)
    if m:
        dd, mm, yyyy = m.groups()
        return f"{yyyy}-{mm}-{dd}"
    return None


def _extract_match_lines(text: str, default_sport: Optional[str] = None, limit: int = 60) -> List[MatchLine]:
    """Достаёт "похожие на матч" строки из текста.

    Поддерживает входы вида:
    - "KHL: Team A — Team B"
    - "Team A - Team B"
    - "match_id=12345 Team A — Team B"
    - карточные форматы (частично)

    Это эвристика: не обязана вытащить 100%, зато не ломается.
    """

    lines = [ln.strip() for ln in _safe_str(text).splitlines() if ln.strip()]
    out: List[MatchLine] = []

    # Паттерн для "A — B" / "A - B" / "A vs B"
    sep = r"(?:\s+[—\-–]\s+|\s+vs\.?\s+|\s+VS\s+)"
    pat = re.compile(rf"^(?:(?P<league>[^|]{2,40})\s*[|:]\s*)?(?P<a>[^\n]{{2,80}}?){sep}(?P<b>[^\n]{{2,80}}?)(?:\s+\((?P<status>[^)]+)\))?$")

    # match_id внутри строки
    id_pat = re.compile(r"\b(?:match[_\s]?id|id)\s*[:=]\s*(\d{4,})\b", re.I)

    for ln in lines:
        if len(out) >= limit:
            break

        # слишком длинные/мусорные строки пропускаем
        if len(ln) > 200:
            continue

        match_id = None
        m_id = id_pat.search(ln)
        if m_id:
            match_id = m_id.group(1)

        m = pat.match(ln)
        if not m:
            continue

        a = (m.group("a") or "").strip(" •-–—")
        b = (m.group("b") or "").strip(" •-–—")
        league = (m.group("league") or "").strip() or None
        status = (m.group("status") or "").strip() or None

        if len(a) < 2 or len(b) < 2:
            continue

        out.append(
            MatchLine(
                sport=default_sport,
                a=a,
                b=b,
                league=league,
                match_id=match_id,
                status=status,
            )
        )

    return out


def _extract_score_and_status(text: str) -> Tuple[Optional[str], Optional[str]]:
    # например: FINISHED 2:1 (FT) / LIVE 1:0 / 2-1
    score = None
    status = None

    m = re.search(r"\b(FINISHED|LIVE|SCHEDULED|CANCELLED|POSTPONED|FT|HT)\b", text, re.I)
    if m:
        status = m.group(1).upper()

    m = re.search(r"\b(\d{1,2})\s*[:\-]\s*(\d{1,2})\b", text)
    if m:
        score = f"{m.group(1)}:{m.group(2)}"

    return score, status


# -----------------------------
# Text builders
# -----------------------------

def _simple_disclaimer() -> str:
    return "\n\nℹ️ Это аналитическая справка, не рекомендация."


def _build_pre(match: MatchLine, raw: str) -> str:
    date = _extract_date(raw)
    title = f"🧠 PRE-обзор\n{match.a} — {match.b}"
    if match.league:
        title += f"\n🏆 {match.league}"
    if date:
        title += f"\n📅 {date}"

    blocks = [
        title,
        "\nЧто проверить перед стартом:",
        "• составы/травмы и кто в воротах (для хоккея)",
        "• мотивация: турнирная ситуация, серия игр, дерби/кубок",
        "• календарь: 2-я игра за 2–3 дня, перелёты",
        "• рынок: были ли резкие движения линии без новостей",
        "\nНа что смотреть по ходу матча:",
        "• темп в первые 10–15 минут: броски/владение/угрозы",
        "• удаления/карточки и качество большинства (для хоккея)",
        "• стандарты и опасные моменты (для футбола)",
        "\nИдеи (без навязывания):",
        "• выбрать один сценарий: быстрый темп → больше событий; низкий темп → меньше событий",
        "• если фаворит выглядит "
        "свежо, а аутсайдер глубоко садится — оценить, хватит ли моментов для гола/шайбы",
        _simple_disclaimer(),
    ]

    return "\n".join(blocks)


def _build_live(match: MatchLine, raw: str) -> str:
    score, status = _extract_score_and_status(raw)

    title = f"🟢 LIVE-обзор\n{match.a} — {match.b}"
    if match.league:
        title += f"\n🏆 {match.league}"
    if score:
        title += f"\n📊 Счёт: {score}"
    if status:
        title += f"\n⏱ Статус: {status}"

    blocks = [
        title,
        "\nЧто важно прямо сейчас:",
        "• кто создаёт моменты: реальные угрозы vs владение ради владения",
        "• дисциплина: удаления/карточки могут резко менять рисунок",
        "• замены/перестройки: после гола часто меняется темп",
        "\nБыстрая логика (простыми словами):",
        "• много моментов и высокий темп → вероятность событий выше",
        "• мало моментов, команды осторожны → событий обычно меньше",
        "\nЕсли сомневаешься — лучше наблюдать ещё 5–10 минут и собрать больше фактов.",
        _simple_disclaimer(),
    ]

    return "\n".join(blocks)


def _pick_top(matches: List[MatchLine], n: int = 3) -> List[MatchLine]:
    """Простой отбор "топ" без внешних данных.

    Эвристика: сначала по "популярным" лигам, затем по порядку.
    """
    if not matches:
        return []

    popular = [
        "khl",
        "nhl",
        "all star",
        "premier league",
        "epl",
        "la liga",
        "serie a",
        "bundesliga",
        "champions league",
        "europa",
        "world cup",
        "euro",
    ]

    def score(m: MatchLine) -> int:
        base = 0
        txt = (m.league or "").lower()
        for i, key in enumerate(popular):
            if key in txt:
                base += 100 - i
        # чуть-чуть поднимаем матчи с известными командами (по длине/наличию заглавных)
        base += 5 if (m.a[:1].isupper() and m.b[:1].isupper()) else 0
        return base

    sorted_matches = sorted(matches, key=score, reverse=True)
    return sorted_matches[:n]


def _build_daily_pro(raw: str) -> str:
    detected_date = _extract_date(raw) or datetime.utcnow().strftime("%Y-%m-%d")

    # Попробуем вытащить матчи из текста.
    base_sport = _guess_sport(raw)
    matches = _extract_match_lines(raw, default_sport=base_sport)

    # Если в одном промпте упоминаются 2 спорта — разделим по ключам.
    # Иначе: если ничего не распарсили — дадим универсальную справку.

    # Пробуем отдельно вытащить хоккей/футбол из общего текста
    hockey = _extract_match_lines(raw, default_sport="ice-hockey") if "хок" in raw.lower() or "ice-hockey" in raw.lower() else []
    football = _extract_match_lines(raw, default_sport="football") if "фут" in raw.lower() or "football" in raw.lower() else []

    # Если отдельные списки пустые, используем общий
    if not hockey and not football:
        if base_sport == "ice-hockey":
            hockey = matches
        elif base_sport == "football":
            football = matches

    # Если всё равно пусто — общий совет
    if not hockey and not football:
        return _truncate(
            "🏒⚽ DAILY PRO\n"
            f"📅 {detected_date}\n\n"
            "Сегодня у меня нет списка матчей в сообщении.\n"
            "Открой «Матчи сегодня» и выбери матч — я дам PRE/LIVE обзор."
            + _simple_disclaimer()
        )

    out: List[str] = [f"🏒⚽ DAILY PRO\n📅 {detected_date}"]

    def section(title: str, ms: List[MatchLine]) -> None:
        top = _pick_top(ms, 3)
        if not top:
            return
        out.append(f"\n🔥 {title}: 3 события дня (для наблюдения)")
        for i, m in enumerate(top, 1):
            line = f"{i}) {m.a} — {m.b}"
            if m.league:
                line += f"\n   🏆 {m.league}"
            out.append(line)
        out.append("\nЧто смотреть (просто):")
        out.append("• новости по составам/травмам перед стартом")
        out.append("• изменения темпа в первые минуты")
        out.append("• резкие движения линии без видимых причин (может быть инфо)")

    section("Хоккей", hockey)
    section("Футбол", football)

    out += [
        "\n🧩 Как использовать (без навязывания):",
        "• выбери 1–2 матча, дождись составов и первых минут игры",
        "• если картинка не подтверждается — просто пропусти",
        "\n⛔ Когда лучше не лезть:",
        "• нет инфы по составам / много ротации",
        "• слишком хаотичная игра (сложно читать)",
        "• линия сильно "
        "скачет без новостей",
        _simple_disclaimer(),
    ]

    return _truncate("\n".join(out))


def _detect_intent(raw: str) -> str:
    low = raw.lower()
    if "daily" in low or "охотник дня" in low or "топ-3" in low:
        return "daily"
    if "live" in low or "лайв" in low or "inplay" in low:
        return "live"
    if "pre" in low or "пре" in low or "прематч" in low:
        return "pre"
    # иногда кнопка PRE/LIVE передаёт маркер вида "MODE:PRE" или "MODE:LIVE"
    if "mode:live" in low:
        return "live"
    if "mode:pre" in low:
        return "pre"
    return "daily"  # безопасный дефолт


# -----------------------------
# Public API
# -----------------------------

async def run_dialog_agent(user_id: int, text: str) -> str:
    """Единая точка входа для "AI".

    На вход приходит произвольный текст (промпт), уже собранный приложением.
    Мы определяем режим и возвращаем готовый ответ.
    """

    raw = _safe_str(text)
    intent = _detect_intent(raw)

    # Постараемся извлечь хотя бы один матч из текста.
    sport = _guess_sport(raw)
    matches = _extract_match_lines(raw, default_sport=sport, limit=10)
    match = matches[0] if matches else MatchLine(sport=sport, a="Матч", b="—", league=None)

    if intent == "pre":
        return _truncate(_build_pre(match, raw))
    if intent == "live":
        return _truncate(_build_live(match, raw))

    # daily
    return _truncate(_build_daily_pro(raw))


__all__ = ["run_dialog_agent"]
