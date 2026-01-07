# src/ui_text.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ============================================================
# ЕДИНЫЙ СТИЛЬ ТЕКСТОВ ДЛЯ UI (MVP → production friendly)
# Цель:
# - меньше “раздражающих” сообщений
# - единая структура экранов
# - компактные paywall/ошибки
# - безопасный дисклеймер (аналитика ≠ рекомендация)
# ============================================================

DISCLAIMER = "ℹ️ Аналитический материал. Не является рекомендацией."


def _cap(s: str) -> str:
    return (s or "").strip()


def _join(*parts: str) -> str:
    lines: list[str] = []
    for p in parts:
        p = _cap(p)
        if not p:
            continue
        lines.extend(p.splitlines())
    # убираем лишние пустые строки по краям
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _section(title: str, bullets: list[str], *, max_items: int = 6) -> str:
    bullets = [b.strip() for b in (bullets or []) if b and b.strip()]
    if not bullets:
        return ""
    bullets = bullets[:max_items]
    lines = [title]
    lines += [f"• {b}" for b in bullets]
    return "\n".join(lines)


@dataclass(frozen=True)
class MatchMeta:
    sport: str
    title: str
    league: str
    match_id: str


# -----------------------------
# Экран: Матч (хаб)
# -----------------------------
def match_hub(meta: MatchMeta) -> str:
    return _join(
        f"🏟 {meta.title} ({meta.league})",
        "",
        "Выбери раздел:",
        "• Pre — логика линии до матча",
        "• LIVE — разбор по ходу матча",
        "",
        DISCLAIMER,
    )


# -----------------------------
# Pre: overview
# -----------------------------
def pre_overview(meta: MatchMeta) -> str:
    return _join(
        f"📊 Pre-обзор • {meta.title} ({meta.league})",
        "",
        "Что смотреть в линии:",
        "• кто «тащит» ожидания (фаворит/андердог) и почему",
        "• где рынок ждёт голы/очки (темп и характер матча)",
        "• как связаны 1X2, тотал и фора",
        "",
        "Выбери рынок ниже: 1X2 / Тотал / Фора / Связки",
        "",
        DISCLAIMER,
    )


# -----------------------------
# Pre: 1X2
# -----------------------------
def pre_moneyline(meta: MatchMeta, *, home: Optional[float] = None, draw: Optional[float] = None, away: Optional[float] = None) -> str:
    odds_block = ""
    if home is not None and away is not None:
        if draw is None:
            odds_block = f"Коэффициенты: П1 {home} • П2 {away}"
        else:
            odds_block = f"Коэффициенты: П1 {home} • X {draw} • П2 {away}"

    return _join(
        f"🧠 Pre: 1X2 • {meta.title} ({meta.league})",
        odds_block,
        "",
        "Смысл рынка:",
        "• 1X2 — «кто сильнее» с учётом контекста и ожиданий",
        "",
        "Как читать движение:",
        "• к фавориту — рынок усиливает вероятность доминирования",
        "• к андердогу — рынок закладывает сопротивление/равный темп",
        "",
        "На что обратить внимание:",
        "• перекос в одну сторону без поддержки тотала/форы",
        "• резкие изменения ближе к старту (новости/состав/мотивация)",
        "",
        DISCLAIMER,
    )


# -----------------------------
# Pre: Total
# -----------------------------
def pre_total(meta: MatchMeta, *, total_value: Optional[float] = None, over: Optional[float] = None, under: Optional[float] = None) -> str:
    odds_block = ""
    if total_value is not None:
        if over is not None and under is not None:
            odds_block = f"Линия: ТБ/ТМ {total_value} • Б {over} • М {under}"
        else:
            odds_block = f"Линия: тотал {total_value}"

    return _join(
        f"🧠 Pre: Тотал • {meta.title} ({meta.league})",
        odds_block,
        "",
        "Смысл рынка:",
        "• тотал — ожидание темпа и количества моментов/владения",
        "",
        "Как читать движение:",
        "• тотал вверх — рынок ждёт более открытый сценарий",
        "• тотал вниз — ждут осторожность/низкий темп/плотную оборону",
        "",
        "Проверка на адекватность:",
        "• тотал вверх + фаворит укрепляется → сценарий «доминирование и голы/очки»",
        "• тотал вверх, но 1X2 не двигается → рынок ждёт обоюдоострый матч",
        "",
        DISCLAIMER,
    )


# -----------------------------
# Pre: Handicap
# -----------------------------
def pre_handicap(meta: MatchMeta, *, team: Optional[str] = None, handicap_value: Optional[float] = None, odds: Optional[float] = None) -> str:
    odds_block = ""
    if handicap_value is not None:
        team_label = "хозяева" if (team == "home") else ("гости" if (team == "away") else "команда")
        if odds is not None:
            odds_block = f"Линия: фора {team_label} {handicap_value} • кф {odds}"
        else:
            odds_block = f"Линия: фора {team_label} {handicap_value}"

    return _join(
        f"🧠 Pre: Фора • {meta.title} ({meta.league})",
        odds_block,
        "",
        "Смысл рынка:",
        "• фора — «насколько» один сильнее другого в ожидаемом сценарии",
        "",
        "Как читать движение:",
        "• фора в минус усиливается — рынок ждёт преимущество/контроль",
        "• фора смягчается — ждут более равный матч или «качели»",
        "",
        "Сигналы, что сценарий меняется:",
        "• 1X2 двигается, а фора стоит — сомнения в разнице классов",
        "• фора двигается сильнее, чем 1X2 — рынок «перекладывает» ожидания в разницу",
        "",
        DISCLAIMER,
    )


# -----------------------------
# Pre: Links (связки рынков)
# -----------------------------
def pre_links(meta: MatchMeta) -> str:
    return _join(
        f"🔗 Связки рынков • {meta.title} ({meta.league})",
        "",
        "Идея:",
        "• рынки описывают один сценарий разными словами",
        "",
        "Частые связки:",
        "• фаворит усиливается + тотал вверх → доминирование и темп",
        "• фаворит усиливается + тотал вниз → контроль, но аккуратно",
        "• андердог укрепляется + тотал вверх → обоюдоострая игра",
        "• тотал вниз + фора смягчается → осторожный и равный матч",
        "",
        "Как использовать:",
        "• если один рынок «кричит», а остальные молчат — это повод задуматься о причине",
        "",
        DISCLAIMER,
    )


# -----------------------------
# LIVE: overview (коротко)
# -----------------------------
def live_overview(meta: MatchMeta) -> str:
    return _join(
        f"🟢 LIVE-обзор • {meta.title} ({meta.league})",
        "",
        "Что меняется в LIVE:",
        "• темп (ускорение/замедление)",
        "• структура (кто контролирует мяч/территорию/инициативу)",
        "• реакция на ключевые события (гол, удаление, тайм-аут)",
        "",
        "Нажми «LIVE (полный)» — дам разбор сценариев и связок.",
        "",
        DISCLAIMER,
    )


# -----------------------------
# LIVE: full (глубже, но без чисел)
# -----------------------------
def live_full(meta: MatchMeta) -> str:
    return _join(
        f"🟢 LIVE (полный) • {meta.title} ({meta.league})",
        "",
        "Сценарий матча сейчас:",
        "• кто навязывает рисунок",
        "• насколько игра «открытая» или «закрытая»",
        "",
        "Логика линии (без чисел):",
        "• тотал: вверх / вниз / ровно — по темпу и качеству моментов",
        "• фора: усиливается / смягчается — по контролю и устойчивости преимущества",
        "",
        "Риски интерпретации:",
        "• один эпизод может временно исказить линию",
        "• «шум» от серий моментов без реального перелома",
        "",
        DISCLAIMER,
    )


# -----------------------------
# Throttle / мягкие ошибки
# -----------------------------
def soft_throttle() -> str:
    return _join(
        "⏳ Слишком часто.",
        "Дай пару секунд — и нажми ещё раз.",
    )


def ai_fallback_pre(meta: Optional[MatchMeta] = None) -> str:
    title = "📊 Обзор рынков"
    if meta:
        title = f"📊 Pre-обзор • {meta.title} ({meta.league})"
    return _join(
        title,
        "",
        "Сейчас AI недоступен — показываю базовую структуру.",
        "",
        "Что обычно важно:",
        "• 1X2 — баланс сил и ожиданий",
        "• тотал — темп и открытость",
        "• фора — ожидаемая разница",
        "",
        DISCLAIMER,
    )


def ai_fallback_live(meta: Optional[MatchMeta] = None) -> str:
    title = "🟢 LIVE-обзор"
    if meta:
        title = f"🟢 LIVE-обзор • {meta.title} ({meta.league})"
    return _join(
        title,
        "",
        "Сейчас AI недоступен — даю краткую памятку LIVE.",
        "",
        "Что отслеживать:",
        "• темп и структура",
        "• устойчивость преимущества",
        "• реакция на ключевые события",
        "",
        DISCLAIMER,
    )


# -----------------------------
# Paywall (без раздражения)
# -----------------------------
def paywall_live() -> str:
    return _join(
        "🟢 LIVE доступен в Premium.",
        "",
        "Откроется:",
        "• LIVE-обзор и LIVE (полный)",
        "• обновления без лимитов",
        "",
        "Нажми «Активировать Premium».",
        "",
        DISCLAIMER,
    )


def paywall_live_refresh() -> str:
    return _join(
        "🔄 Обновления LIVE ограничены на Free.",
        "",
        "В Premium — обновления без лимитов.",
        "Нажми «Активировать Premium».",
        "",
        DISCLAIMER,
    )
