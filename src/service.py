from __future__ import annotations

import logging
import os
import re
import inspect
from datetime import datetime, timedelta
from typing import List

from fastapi import FastAPI, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from .db import init_db, get_session, User
from .bets_db import (
    get_user_stats,
    add_bet,
    get_last_bets,
    settle_bet,
    get_user_bank,
    set_user_bank,
    change_user_bank,
    get_all_bets,
)
from .hockey_logic import khl_today_text_from_winline, build_match_context_notes
from .khl_form_client import (
    get_team_form,
    TeamForm,
    TeamAdvancedForm,
)
from .winline_client import get_khl_events_today  # для анализа матча по id

logger = logging.getLogger(__name__)


# ===================== ПРЕМИУМ =====================


def is_premium(session: Session, user_id: int) -> bool:
    """
    Простая проверка: активен ли премиум у пользователя.
    """
    user = session.get(User, user_id)
    if not user or not getattr(user, "premium_until", None):
        return False
    return user.premium_until > datetime.utcnow()


# ===================== ОТЧЁТ ЗА НЕДЕЛЮ =====================


def build_weekly_report(session: Session, user_id: int) -> str:
    """
    Недельный отчёт с фокусом на дисциплину и риск-менеджмент.
    """
    stats = get_user_stats(session, user_id)

    # 0) Совсем пустой профиль
    if stats.total_bets == 0:
        return (
            "✨ Недельный отчёт\n\n"
            "Пока у тебя нет ни одной сохранённой ставки.\n"
            "Начни с первой: 'ставка 1000 на СКА тотал больше 5.5 за 1.9', "
            "а я дальше посчитаю статистику и покажу твою динамику."
        )

    # 1) Ставки есть, но ещё не рассчитаны
    if stats.settled_bets == 0 and stats.pushes == 0:
        return (
            "✨ Недельный отчёт\n\n"
            f"Всего ставок за неделю: {stats.total_bets}\n"
            "Пока ни одна ставка не отмечена как win/lose.\n"
            "Когда зафиксируешь результаты (например: 'ставка 1 выиграла'), "
            "я посчитаю винрейт, ROI и покажу, как ты идёшь по дистанции."
        )

    lines: list[str] = []
    lines.append("✨ Недельный отчёт по твоей игре\n")

    # Базовые цифры
    lines.append(f"Всего ставок: {stats.total_bets}")
    lines.append(f"Рассчитано (win/lose): {stats.settled_bets}")
    if stats.pushes:
        lines.append(f"Возвратов: {stats.pushes}")
    lines.append(f"Винрейт: {stats.winrate:.1f}%")
    lines.append(f"ROI: {stats.roi:.2f}%")
    lines.append(f"Плюс/минус за неделю: {stats.pnl:.0f}")
    lines.append(f"Общий объём ставок: {stats.total_stake:.0f}")

    bank = get_user_bank(session, user_id)
    if bank is not None and stats.total_bets > 0:
        avg_stake = stats.total_stake / stats.total_bets
        stake_pct = avg_stake / bank * 100 if bank > 0 else 0.0

        lines.append("")
        lines.append("💰 Нагрузка на банк:")
        lines.append(f"Средний размер ставки: {avg_stake:.0f} ({stake_pct:.1f}% от банка).")

        if stake_pct <= 1:
            lines.append(
                "Ты играешь очень консервативно. Это безопасно, но профит будет приходить медленнее."
            )
        elif 1 < stake_pct <= 3:
            lines.append(
                "Оптимальный уровень риска: 1–3% от банка. Хороший баланс между безопасностью и ростом."
            )
        elif 3 < stake_pct <= 6:
            lines.append(
                "Ты играешь агрессивно (выше 3% от банка). На дистанции такие нагрузки могут давать "
                "глубокие просадки — стоит подумать о снижении среднего %."
            )
        else:
            lines.append(
                "Очень высокая нагрузка на банк. Такие размеры ставок больше похожи на азартную игру, "
                "а не на управляемую стратегию. Профессиональная игра — это обычно до 3% от банка."
            )

    lines.append("")
    lines.append("🧠 Совет недели:")
    if stats.roi < 0:
        lines.append(
            "Неделя в лёгком минусе. Главное сейчас — не увеличивать размер ставки, "
            "а сохранить дисциплину и продолжать играть по модели, а не по эмоциям."
        )
    else:
        lines.append(
            "Неделя в плюсе. Важно не зазнаваться: сохраняй тот же размер ставки и подход, "
            "который дал результат, и не начинай 'залетать' повышенными суммами."
        )

    lines.append(
        "\nДальше можно углубиться:\n"
        "• 'лучшая ставка недели' — что у тебя сработало лучше всего\n"
        "• 'ошибка недели' — где ты потерял больше всего\n"
        "• 'разбор моих рынков' — какие типы ставок тянут тебя вверх/вниз"
    )

    return "\n".join(lines)


# ===================== ОТЧЁТ ЗА МЕСЯЦ =====================


def build_monthly_report(session: Session, user_id: int) -> str:
    """
    Отчёт за последние 30 дней по ставкам пользователя.
    """
    from .bets_db import get_all_bets  # локальный импорт

    today = datetime.utcnow()
    period_start = today - timedelta(days=30)

    bets_all = get_all_bets(session, user_id) or []

    bets = [
        b
        for b in bets_all
        if getattr(b, "created_at", None) is not None
        and b.created_at >= period_start
    ]

    if not bets:
        return (
            "За последние 30 дней у тебя не было записанных ставок. "
            "Как только набросаешь историю за месяц, я соберу подробный отчёт."
        )

    settled = [b for b in bets if getattr(b, "result", None) in ("win", "lose")]
    pushes = [b for b in bets if getattr(b, "result", None) == "push"]
    wins = [b for b in settled if b.result == "win"]

    total_bets = len(bets)
    settled_count = len(settled)
    pushes_count = len(pushes)

    total_stake = sum(
        float(b.stake) for b in bets if getattr(b, "stake", None) is not None
    )
    total_pnl = sum(
        float(b.profit) for b in bets if getattr(b, "profit", None) is not None
    )

    winrate = (len(wins) / settled_count * 100.0) if settled_count > 0 else 0.0
    roi = (total_pnl / total_stake * 100.0) if total_stake > 0 else 0.0

    bets_with_profit = [
        b for b in bets if getattr(b, "profit", None) is not None
    ]
    best_bet = max(bets_with_profit, key=lambda b: b.profit) if bets_with_profit else None
    worst_bet = min(bets_with_profit, key=lambda b: b.profit) if bets_with_profit else None

    period_str = f"{period_start:%d.%m}–{today:%d.%m}"

    lines: list[str] = []
    lines.append(f"📈 Отчёт за последние 30 дней ({period_str}):")
    lines.append(f"Всего ставок: {total_bets}")
    lines.append(f"Рассчитано (win/lose): {settled_count}")
    if pushes_count:
        lines.append(f"Возвратов: {pushes_count}")
    lines.append(f"Винрейт: {winrate:.1f}%")
    lines.append(f"ROI: {roi:.2f}%")

    sign_pnl = "+" if total_pnl >= 0 else ""
    lines.append(f"PnL за период: {sign_pnl}{total_pnl:.0f}")
    lines.append(f"Общий объём ставок: {total_stake:.0f}")

    if best_bet is not None:
        lines.append("")
        lines.append("🏆 Лучшая ставка месяца:")
        if getattr(best_bet, "created_at", None):
            lines.append(f"• Дата: {best_bet.created_at:%d.%m %H:%M}")
        if getattr(best_bet, "raw_text", None):
            lines.append(f"• {best_bet.raw_text}")
        lines.append(f"• Результат: +{best_bet.profit:.0f}")

    if worst_bet is not None and worst_bet is not best_bet:
        lines.append("")
        lines.append("⚠️ Самая слабая ставка месяца:")
        if getattr(worst_bet, "created_at", None):
            lines.append(f"• Дата: {worst_bet.created_at:%d.%m %H:%M}")
        if getattr(worst_bet, "raw_text", None):
            lines.append(f"• {worst_bet.raw_text}")
        sign_w = "+" if worst_bet.profit >= 0 else ""
        lines.append(f"• Результат: {sign_w}{worst_bet.profit:.0f}")
        lines.append(
            "Важно не просто зафиксировать минус, а понять причину: "
            "переоценил команду, зашёл в неудобный рынок или перегрузил банк."
        )

    lines.append("")
    lines.append("🧠 Вывод по месяцу:")
    if roi >= 0:
        lines.append(
            "Месяц в целом в плюсе или около нуля. Это хороший знак: у тебя уже есть рабочие "
            "паттерны. Задача — отфильтровать лишний мусор и усиливать сильные стороны."
        )
    else:
        lines.append(
            "Месяц в минусе. Это не приговор, а материал для работы: важно посмотреть, "
            "какие именно типы ставок тянут результат вниз, и скорректировать стратегию."
        )

    lines.append(
        "\nЧтобы углубиться, попробуй:\n"
        "• 'лучшая ставка недели' — короткий горизонт\n"
        "• 'ошибка недели' — свежие ошибки\n"
        "• 'разбор моих рынков' — какие типы рынков тебя тянут вверх/вниз"
    )

    return "\n".join(lines)


# ===================== SAFE-ХЕЛПЕРЫ ДЛЯ ФОРМЫ =====================


async def get_team_form_safe(team_name: str) -> TeamForm | None:
    try:
        if inspect.iscoroutinefunction(get_team_form):
            return await get_team_form(team_name)
        return get_team_form(team_name)
    except Exception:
        logger.exception("Ошибка при получении формы команды %s", team_name)
        return None


def get_team_advanced_form_safe(team_name: str) -> TeamAdvancedForm | None:
    """
    Заглушка для PRO-формы команды.
    """
    return None


# ===================== РАЗБОР МАТЧА КХЛ =====================


def build_khl_match_analysis(ev) -> str:
    """
    Базовый и максимально устойчивый разбор матча КХЛ.
    """
    team1_name = getattr(ev, "team1", "Команда 1")
    team2_name = getattr(ev, "team2", "Команда 2")
    event_id = getattr(ev, "id", "—")

    # 1. Находим рынок 1X2
    market_1x2 = None
    for m in getattr(ev, "markets", []) or []:
        name = (getattr(m, "name", "") or "").upper()
        if name in ("1X2", "1X", "3WAY", "3-WAY"):
            market_1x2 = m
            break

    if not market_1x2:
        return (
            f"📊 Разбор матча КХЛ:\n"
            f"{team1_name} — {team2_name} (id: {event_id})\n\n"
            "Я не нашёл рынок 1X2 по этому матчу. "
            "Попробуй другой матч или позже — возможно, линия ещё не выставлена."
        )

    # 2. Собираем коэффициенты 1 / X / 2
    odds_map: dict[str, float] = {}
    for o in getattr(market_1x2, "outcomes", []) or []:
        key = (getattr(o, "name", "") or "").strip()
        price = getattr(o, "price", None)
        if not key or price is None:
            continue
        try:
            odds_map[key] = float(price)
        except (TypeError, ValueError):
            continue

    def _pick_odds(*names: str) -> float | None:
        for n in names:
            if n in odds_map:
                return odds_map[n]
        return None

    odds_1 = _pick_odds("1", "HOME")
    odds_x = _pick_odds("X", "DRAW")
    odds_2 = _pick_odds("2", "AWAY")

    if odds_1 is None or odds_x is None or odds_2 is None:
        lines = [
            "📊 Разбор матча КХЛ:",
            f"{team1_name} — {team2_name} (id: {event_id})",
            "",
            "Не удалось корректно прочитать все три коэффициента 1X2.",
            "Показываю только те исходы, которые нашёл:",
        ]
        for k, v in odds_map.items():
            lines.append(f"• {k}: кэф {v:.2f}")
        lines.append("")
        lines.append(
            "Используй эти коэффициенты как ориентир. "
            "Для value-чека можешь прогонять конкретный кэф через команды вида "
            "'value 1.85' или 'проверка кэф 2.3'."
        )
        return "\n".join(lines)

    # 3. Имплайд-вероятности и маржа
    imp_1 = 100.0 / odds_1
    imp_x = 100.0 / odds_x
    imp_2 = 100.0 / odds_2
    imp_sum = imp_1 + imp_x + imp_2
    margin = imp_sum - 100.0

    if imp_sum > 0:
        fair_1 = imp_1 / imp_sum * 100.0
        fair_x = imp_x / imp_sum * 100.0
        fair_2 = imp_2 / imp_sum * 100.0
    else:
        fair_1 = fair_x = fair_2 = 0.0

    lines: list[str] = []
    lines.append("📊 Разбор матча КХЛ:")
    lines.append(f"{team1_name} — {team2_name} (id: {event_id})")
    lines.append("")
    lines.append("Линия 1X2 (коэффициенты и имплайд-вероятности):")
    lines.append(f"• 1: кэф {odds_1:.2f}, импл. вероятность ≈ {imp_1:.1f}%")
    lines.append(f"• X: кэф {odds_x:.2f}, импл. вероятность ≈ {imp_x:.1f}%")
    lines.append(f"• 2: кэф {odds_2:.2f}, импл. вероятность ≈ {imp_2:.1f}%")
    lines.append("")
    lines.append(f"Маржа букмекера по рынку 1X2 ≈ {margin:.1f} п.п.")
    lines.append("")
    lines.append("Оценка 'честных' вероятностей (без маржи бука):")
    lines.append(f"• 1: ≈ {fair_1:.1f}%")
    lines.append(f"• X: ≈ {fair_x:.1f}%")
    lines.append(f"• 2: ≈ {fair_2:.1f}%")
    lines.append("")
    lines.append(
        "Как использовать:\n"
        "1) Определи, какой исход ты вообще рассматриваешь (1 / X / 2).\n"
        "2) Прогоняй кэф через команды вида 'value 1.85' или 'есть ли value в ставке по 2.10' — "
        "я переведу его в вероятность и дам чек-лист по value.\n"
        "3) Сравни свою оценку шансов с тем, что закладывает рынок."
    )

    # 4. Турнирный контекст и мотивация
    try:
        ctx = build_match_context_notes(team1_name, team2_name, league="KHL")
    except Exception:
        ctx = ""

    if ctx:
        lines.append("")
        lines.append("📌 Турнирный контекст и мотивация:")
        lines.append(ctx)

    lines.append("")
    lines.append(
        "Это не прогноз и не команда 'ставить/не ставить', а рабочий чек-лист по линии. "
        "Финальное решение всегда за тобой."
    )

    return "\n".join(lines)


# ===================== FASTAPI ПРИЛОЖЕНИЕ =====================


app = FastAPI(title="KHL AI Betting Agent API")


class AgentQuery(BaseModel):
    user_id: int
    message: str


class AgentResponse(BaseModel):
    reply: str


@app.on_event("startup")
def on_startup() -> None:
    logging.basicConfig(level=logging.INFO)
    init_db()
    logger.info("FastAPI сервис запущен (бот работает в отдельном Worker).")


# ВАЖНО: healthcheck для Render — должен отвечать и на GET, и на HEAD
@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"status": "ok", "service": "khl-agent-api"}


# ===================== API-ЭНДПОИНТЫ ДЛЯ АГЕНТА =====================


@app.post("/agent/query", response_model=AgentResponse)
async def agent_query(
    payload: AgentQuery,
    session: Session = Depends(get_session),
) -> AgentResponse:
    reply_text = await run_agent(
        user_id=payload.user_id,
        message=payload.message,
        session=session,
    )
    return AgentResponse(reply=reply_text)


@app.get("/agent/last-bets")
async def agent_last_bets(
    user_id: int,
    limit: int = 5,
    session: Session = Depends(get_session),
):
    bets = get_last_bets(session, user_id, limit=limit)

    out: list[dict] = []
    for b in bets:
        out.append(
            {
                "id": b.id,
                "created_at": b.created_at.isoformat() if b.created_at else None,
                "raw_text": b.raw_text,
                "event": b.event,
                "outcome": b.outcome,
                "stake": float(b.stake) if b.stake is not None else None,
                "odds": float(b.odds) if b.odds is not None else None,
                "result": b.result,
                "profit": float(b.profit) if b.profit is not None else None,
            }
        )

    return {"bets": out}

# ДАЛЬШЕ — ВЕСЬ ТВОЙ ОСТАЛЬНЫЙ КОД (всё, что уже было ниже в старом service.py):
# парсинг ставок, отчёты, build_user_profile, build_value_analysis, build_express_evaluation
# и в самом конце — функция run_agent(...)
# Я его здесь не дублирую повторно, чтобы ответ не раздувать,
# но в файле у тебя он уже есть — просто НЕ удаляй его.

# ===================== ГЛАВНЫЙ АГЕНТ =====================
# (здесь должен быть твой run_agent(...) без изменений)
