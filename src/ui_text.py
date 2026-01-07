# src/ui_text.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


DISCLAIMER = "ℹ️ Аналитический материал. Не является рекомендацией."


def _safe(s: Optional[str]) -> str:
    return (s or "").strip()


def _fmt_dt_msk(date_str: Optional[str] = None, time_str: Optional[str] = None) -> str:
    """
    Простой форматтер. Ты можешь уже отдавать date/time готовыми строками.
    """
    ds = _safe(date_str)
    ts = _safe(time_str)
    if ds and ts:
        return f"🗓 {ds} • ⏱ {ts} (МСК)"
    if ds:
        return f"🗓 {ds} (МСК)"
    return ""


@dataclass
class MatchCard:
    match_id: str
    home: str
    away: str
    league: str = ""
    date_str: Optional[str] = None  # "2026-01-07"
    time_str: Optional[str] = None  # "19:30"


@dataclass
class LiveState:
    live_time: str = "—"          # "57’" / "12:34" / "2P 08:11"
    score: str = "—"              # "1–0"
    pace: Optional[str] = None
    edge: Optional[str] = None
    chances: Optional[str] = None


@dataclass
class PreSignals:
    context_1: Optional[str] = None
    style_1: Optional[str] = None
    risk_1: Optional[str] = None

    ml_hint: Optional[str] = None
    total_hint: Optional[str] = None
    hcp_hint: Optional[str] = None


@dataclass
class ScenarioBlock:
    a_trigger: Optional[str] = None
    a_inplay: Optional[str] = None
    a_breaker: Optional[str] = None

    b_trigger: Optional[str] = None
    b_inplay: Optional[str] = None
    b_breaker: Optional[str] = None

    total_confirm: Optional[str] = None
    hcp_confirm: Optional[str] = None


@dataclass
class TotalBlock:
    pace: Optional[str] = None
    scoring_model: Optional[str] = None
    risk_window: Optional[str] = None

    early_event_effect: Optional[str] = None
    dry_phase_effect: Optional[str] = None
    rotation_effect: Optional[str] = None

    link_1: Optional[str] = None
    link_2: Optional[str] = None


@dataclass
class HandicapBlock:
    edge_side: Optional[str] = None
    edge_strength: Optional[str] = None
    edge_visual: Optional[str] = None

    consistency_note: Optional[str] = None
    total_alignment: Optional[str] = None


@dataclass
class LinksBlock:
    # Можно расширять — сейчас текст самодостаточный
    pass


# -----------------------------
# 0) Матч
# -----------------------------
def text_match(card: MatchCard) -> str:
    dt_line = _fmt_dt_msk(card.date_str, card.time_str)
    header = f"🏟 {card.home} — {card.away}"
    if _safe(card.league):
        header += f" ({card.league})"

    parts = [
        header,
        dt_line,
        "",
        "Выбери, что открыть:",
        "• 📊 Обзор рынков — быстрый снимок и ключевые риски",
        "• 🧠 1X2 / Тотал / Фора — сценарии и что “вшито” в линию",
        "• 🔗 Связки — как рынки подтверждают один сценарий",
        "• 🟢 LIVE — динамика матча (Premium)",
        "",
        DISCLAIMER,
    ]
    # выкидываем пустые строки (кроме намеренных)
    return "\n".join([p for p in parts if p is not None])


# -----------------------------
# 1) Pre overview
# -----------------------------
def text_pre_overview(card: MatchCard, s: Optional[PreSignals] = None, *, fallback: bool = False) -> str:
    title = f"📊 Обзор рынков\n{card.home} — {card.away}"
    if _safe(card.league):
        title += f" ({card.league})"

    if fallback or s is None:
        return "\n".join(
            [
                title,
                "",
                "Быстрый ориентир:",
                "• Рынки чаще всего “закладывают” ожидаемый темп и преимущество одной из сторон.",
                "• Без свежих данных точность ниже — держим фокус на рисках.",
                "",
                "Риски:",
                "• Неизвестные составы/ротация",
                "• Ранний гол меняет сценарий",
                "• Высокая чувствительность к удалению/травме",
                "",
                "Что дальше:",
                "• 🧠 1X2 — сценарий по исходу",
                "• 🧠 Тотал — логика темпа",
                "• 🧠 Фора — где перекос",
                "",
                DISCLAIMER,
            ]
        )

    # подставляем мягкие дефолты, чтобы текст всегда был красивый
    context_1 = _safe(s.context_1) or "кто и за счёт чего должен вести сценарий"
    style_1 = _safe(s.style_1) or "темп и стиль команды / матч-ап"
    risk_1 = _safe(s.risk_1) or "главные факторы, которые ломают план игры"

    ml_hint = _safe(s.ml_hint) or "показывает “кто ведёт сценарий”"
    total_hint = _safe(s.total_hint) or "про темп и количество моментов"
    hcp_hint = _safe(s.hcp_hint) or "проверка силы преимущества"

    return "\n".join(
        [
            title,
            "",
            "Что важно перед матчем:",
            f"• Контекст: {context_1}",
            f"• Темп/стиль: {style_1}",
            f"• Уязвимости: {risk_1}",
            "",
            "Как читать линию:",
            f"• 1X2: {ml_hint}",
            f"• Тотал: {total_hint}",
            f"• Фора: {hcp_hint}",
            "",
            "Что дальше:",
            "• Если ждёшь осторожный матч → открой 🧠 Тотал",
            "• Если важен “кто заберёт сценарий” → 🧠 1X2",
            "• Если ищешь перекос → 🧠 Фора / 🔗 Связки",
            "",
            DISCLAIMER,
        ]
    )


# -----------------------------
# 2) Pre 1X2
# -----------------------------
def text_pre_1x2(card: MatchCard, sc: Optional[ScenarioBlock] = None, *, fallback: bool = False) -> str:
    title = f"🧠 1X2 — сценарии матча\n{card.home} — {card.away}"

    if fallback or sc is None:
        return "\n".join(
            [
                title,
                "",
                "Как читать:",
                "• 1X2 отвечает на вопрос “кто ведёт сценарий”.",
                "• Если рынок ждёт доминирование — это обычно видно и в форе.",
                "• Если рынок ждёт вязкую игру — это чаще подтверждается тоталом.",
                "",
                "Что дальше:",
                "• 🧠 Фора — проверка силы сценария",
                "• 🧠 Тотал — проверка темпа",
                "",
                DISCLAIMER,
            ]
        )

    a_trigger = _safe(sc.a_trigger) or "фаворит быстро забирает инициативу"
    a_inplay = _safe(sc.a_inplay) or "давление, серии атак, контроль темпа"
    a_breaker = _safe(sc.a_breaker) or "ранний гол в другую сторону / красная / травма"

    b_trigger = _safe(sc.b_trigger) or "матч не раскрывается, темп сдержанный"
    b_inplay = _safe(sc.b_inplay) or "мало моментов, много позиционной игры"
    b_breaker = _safe(sc.b_breaker) or "быстрый обмен голами, игра ломается"

    total_confirm = _safe(sc.total_confirm) or "темп подтверждает выбранный сценарий"
    hcp_confirm = _safe(sc.hcp_confirm) or "фора согласована с силой преимущества"

    return "\n".join(
        [
            title,
            "",
            "Сценарий А (через фаворита):",
            f"• Что должно случиться: {a_trigger}",
            f"• Как это выглядит в игре: {a_inplay}",
            f"• Что ломает сценарий: {a_breaker}",
            "",
            "Сценарий B (через андердога/ничью):",
            f"• Что должно случиться: {b_trigger}",
            f"• Как это выглядит: {b_inplay}",
            f"• Что ломает: {b_breaker}",
            "",
            "Проверка через другие рынки:",
            f"• Тотал подтверждает: {total_confirm}",
            f"• Фора подтверждает: {hcp_confirm}",
            "",
            DISCLAIMER,
        ]
    )


# -----------------------------
# 3) Pre Total
# -----------------------------
def text_pre_total(card: MatchCard, t: Optional[TotalBlock] = None, *, fallback: bool = False) -> str:
    title = f"🧠 Тотал — логика темпа\n{card.home} — {card.away}"

    if fallback or t is None:
        return "\n".join(
            [
                title,
                "",
                "Ориентир:",
                "• Тотал — это ставка на темп и количество моментов.",
                "• Главный риск — раннее событие (гол/удаление), которое меняет план игры.",
                "",
                "Что дальше:",
                "• 🟢 LIVE-обзор (Premium) — когда темп уже виден",
                "• 🔗 Связки — сверка сценария",
                "",
                DISCLAIMER,
            ]
        )

    pace = _safe(t.pace) or "средний / выше среднего"
    scoring_model = _safe(t.scoring_model) or "через качество моментов, а не только количество"
    risk_window = _safe(t.risk_window) or "первые 10–15 минут и концовка тайма"

    early = _safe(t.early_event_effect) or "линия часто перестраивается в сторону темпа"
    dry = _safe(t.dry_phase_effect) or "тотал может “подтягиваться” вниз без моментов"
    rot = _safe(t.rotation_effect) or "смена темпа через замены/ротацию"

    link_1 = _safe(t.link_1) or "если 1X2 перекошен — тотал чаще поддерживает сценарий фаворита"
    link_2 = _safe(t.link_2) or "если фора агрессивная — тотал чаще выше"

    return "\n".join(
        [
            title,
            "",
            "Какой матч “вшит” в линию:",
            f"• Ожидаемый темп: {pace}",
            f"• Модель гола/очков: {scoring_model}",
            f"• Окно риска: {risk_window}",
            "",
            "Триггеры движения тотала:",
            f"• Ранний гол/очко → {early}",
            f"• Затяжная “сухая” фаза → {dry}",
            f"• Замены/ротация → {rot}",
            "",
            "Проверка связками:",
            f"• {link_1}",
            f"• {link_2}",
            "",
            DISCLAIMER,
        ]
    )


# -----------------------------
# 4) Pre Handicap
# -----------------------------
def text_pre_handicap(card: MatchCard, h: Optional[HandicapBlock] = None, *, fallback: bool = False) -> str:
    title = f"🧠 Фора — где перекос\n{card.home} — {card.away}"

    if fallback or h is None:
        return "\n".join(
            [
                title,
                "",
                "Ориентир:",
                "• Фора показывает, насколько рынок уверен в преимуществе.",
                "• Чем агрессивнее фора — тем важнее темп и вероятность раннего преимущества.",
                "",
                "Что дальше:",
                "• 🧠 1X2 — подтверждение сценария",
                "• 🧠 Тотал — подтверждение темпа",
                "",
                DISCLAIMER,
            ]
        )

    edge_side = _safe(h.edge_side) or "одной из сторон"
    edge_strength = _safe(h.edge_strength) or "умеренное"
    edge_visual = _safe(h.edge_visual) or "должно быть видно по инициативе и моментам"

    cons = _safe(h.consistency_note) or "если 1X2 и фора расходятся — это зона риска"
    align = _safe(h.total_alignment) or "тотал должен поддерживать выбранный темп"

    return "\n".join(
        [
            title,
            "",
            "Что означает текущая фора:",
            f"• Рынок ждёт преимущество: {edge_side}",
            f"• Насколько оно “сильное”: {edge_strength}",
            f"• Как это должно выглядеть: {edge_visual}",
            "",
            "Где чаще ошибается рынок:",
            "• Переоценка “имени” vs формы",
            "• Недооценка стиля (темп/матч-ап)",
            "• Раннее событие ломает распределение",
            "",
            "Проверка:",
            f"• 1X2 ↔ фора: {cons}",
            f"• Тотал ↔ сценарий: {align}",
            "",
            DISCLAIMER,
        ]
    )


# -----------------------------
# 5) Pre Links
# -----------------------------
def text_pre_links(card: MatchCard) -> str:
    title = f"🔗 Связки рынков\n{card.home} — {card.away}"
    return "\n".join(
        [
            title,
            "",
            "1) Если фаворит “реальный”",
            "• 1X2 сдвигается → фора становится агрессивнее",
            "• тотал чаще растёт, если фаворит играет в темп",
            "",
            "2) Если матч “вязкий”",
            "• тотал вниз",
            "• 1X2 чаще ближе к равному",
            "• фора становится осторожнее",
            "",
            "3) Если ждём разнос",
            "• фора усиливается",
            "• тотал растёт",
            "• LIVE подтверждает темп уже в первые минуты",
            "",
            "Что делать:",
            "• Выбери одну гипотезу (вязкий / темповый / разнос) и сверяй рынки между собой.",
            "• Если рынки противоречат — это зона риска.",
            "",
            DISCLAIMER,
        ]
    )


# -----------------------------
# 6) LIVE overview
# -----------------------------
def text_live_overview(card: MatchCard, live: LiveState, *, fallback: bool = False) -> str:
    title = f"🟢 LIVE-обзор\n{card.home} — {card.away}\n⏱ {live.live_time} • Счёт: {live.score}"

    if fallback or not (_safe(live.pace) or _safe(live.edge) or _safe(live.chances)):
        return "\n".join(
            [
                title,
                "",
                "Пока без уверенных сигналов:",
                "• Подтверди темп: где и как идёт игра",
                "• Подтверди инициативу: серии атак/моменты",
                "• Отметь “слом сценария”: гол/удаление/травма",
                "",
                "Нажми 🔄 Обновить LIVE через 30–60 сек — появится больше сигналов.",
                "",
                DISCLAIMER,
            ]
        )

    pace = _safe(live.pace) or "—"
    edge = _safe(live.edge) or "—"
    chances = _safe(live.chances) or "—"

    return "\n".join(
        [
            title,
            "",
            "Что видно по игре сейчас:",
            f"• Темп: {pace}",
            f"• Давление/инициатива: {edge}",
            f"• Качество моментов: {chances}",
            "",
            "Ключевые риски:",
            "• Резкое изменение темпа после гола/удаления",
            "• Просадка концентрации в концовках",
            "",
            "Что логично проверять в рынках:",
            "• 1X2: кто реально держит сценарий",
            "• Тотал: темп подтверждается моментами или только владением",
            "• Фора: преимущество устойчивое или “на волне”",
            "",
            DISCLAIMER,
        ]
    )


# -----------------------------
# 7) LIVE full
# -----------------------------
def text_live_full(card: MatchCard, live: LiveState, *, fallback: bool = False) -> str:
    title = f"🟢 LIVE (полный)\n{card.home} — {card.away}\n⏱ {live.live_time} • Счёт: {live.score}"

    if fallback:
        return "\n".join(
            [
                title,
                "",
                "1) Структура матча",
                "• Кто навязывает стиль: —",
                "• Где преимущество: —",
                "• Что изменилось за последние минуты: —",
                "",
                "2) Триггеры на ближайшие 5–10 минут",
                "• Гол/удаление полностью перестраивает темп",
                "• Серия атак часто даёт движение линии быстрее статистики",
                "",
                "3) Рынки и логика",
                "• 1X2: кто держит сценарий",
                "• Тотал: темп подтверждается моментами",
                "• Фора: устойчивость преимущества",
                "",
                DISCLAIMER,
            ]
        )

    # даже если у тебя пока мало данных — лучше дать “структуру”, чем “AI недоступен”
    pace = _safe(live.pace) or "темп оцени по серии атак и переходам"
    edge = _safe(live.edge) or "посмотри, кто чаще проводит атаки в опасных зонах"
    chances = _safe(live.chances) or "оценка по моментам/ударам из опасных позиций"

    return "\n".join(
        [
            title,
            "",
            "1) Структура матча",
            f"• Темп: {pace}",
            f"• Инициатива: {edge}",
            f"• Моменты: {chances}",
            "",
            "2) Триггеры на ближайшие 5–10 минут",
            "• Смена темпа после гола/паузы/замен",
            "• Накопление моментов → рост вероятности события",
            "",
            "3) Рынки и логика",
            "• 1X2: лидер сценария сейчас",
            "• Тотал: подтверждение темпа моментами",
            "• Фора: устойчивость преимущества",
            "",
            DISCLAIMER,
        ]
    )


# -----------------------------
# 8) Paywall LIVE (мягкий)
# -----------------------------
def text_live_paywall() -> str:
    return "\n".join(
        [
            "🔒 LIVE доступен в Premium.",
            "",
            "Что ты получаешь:",
            "• LIVE-обзор и полный разбор",
            "• обновления по кнопке 🔄 без лимитов",
            "• расширенные связки рынков",
            "",
            "Нажми «⭐ Premium» — покажу условия.",
        ]
    )
