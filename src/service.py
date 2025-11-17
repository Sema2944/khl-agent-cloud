# src/service.py

import logging
import threading
import os
import re
from datetime import datetime, timedelta

from fastapi import FastAPI, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from .db import init_db, get_session
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

from .khl_client import get_today_khl_events
from .khl_form_client import get_team_form, TeamForm  # пока не используем, но пусть будет

logger = logging.getLogger(__name__)

app = FastAPI(title="KHL AI Betting Agent")


class AgentQuery(BaseModel):
    user_id: int
    message: str


class AgentResponse(BaseModel):
    reply: str


@app.on_event("startup")
def on_startup() -> None:
    """
    Хук старта FastAPI:
    - инициализируем БД
    - настраиваем базовые логи
    """
    logging.basicConfig(level=logging.INFO)
    init_db()
    logger.info("FastAPI сервис запущен")


@app.get("/")
def root():
    return {"status": "ok", "service": "khl-agent"}


@app.post("/agent/query", response_model=AgentResponse)
async def agent_query(
    payload: AgentQuery,
    session: Session = Depends(get_session),
) -> AgentResponse:
    """
    Главная точка входа для AI-агента.
    Telegram-бот (и любые клиенты) шлют сюда user_id + текст.
    """
    reply_text = await run_agent(
        user_id=payload.user_id,
        message=payload.message,
        session=session,
    )
    return AgentResponse(reply=reply_text)


# ------------------ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ПАРСИНГА ------------------


def _parse_stake_and_odds(raw_text: str) -> tuple[float | None, float | None]:
    """
    Выделяем сумму и коэффициент из произвольной строки.
    """
    num_matches = re.findall(r"(\d+([\.,]\d+)?)", raw_text)
    numbers = [m[0] for m in num_matches]

    stake: float | None = None
    odds: float | None = None

    if numbers:
        try:
            stake = float(numbers[0].replace(",", "."))
        except ValueError:
            stake = None

    # 1) рядом с словами "коэф/кф/кэф/коэфф/коэффициент"
    coef_pattern = re.compile(
        r"(коэф(фициент)?|коeff|коэфф|кэф|кф|коэффициент)\s*[:=]?\s*(\d+([\.,]\d+)?)",
        re.IGNORECASE,
    )
    m_coef = coef_pattern.search(raw_text)
    if m_coef:
        try:
            candidate_odds = float(m_coef.group(3).replace(",", "."))
            if candidate_odds >= 1.01:
                odds = candidate_odds
        except ValueError:
            odds = None

    # 2) конструкции "за 1.85" / "по 1,75"
    if odds is None:
        za_pattern = re.compile(
            r"\b(за|по)\s*(\d+([\.,]\d+)?)",
            re.IGNORECASE,
        )
        m_za = za_pattern.search(raw_text)
        if m_za:
            try:
                candidate_odds = float(m_za.group(2).replace(",", "."))
                if 1.01 <= candidate_odds <= 20:
                    if not (stake is not None and stake >= 50 and candidate_odds == stake):
                        odds = candidate_odds
            except ValueError:
                pass

    # 3) если всё ещё нет кэфа — берём второе число как кэф
    if odds is None and len(numbers) >= 2:
        try:
            candidate_odds = float(numbers[1].replace(",", "."))
            if candidate_odds >= 1.01:
                odds = candidate_odds
        except ValueError:
            odds = None

    return stake, odds


def _parse_outcome_and_event(raw_text: str) -> tuple[str | None, str | None]:
    """
    Пытаемся вытащить:
    - outcome: П1/П2/Х/1X/X2/12, тотал, фора и т.п.
    - event: текст о матче/командах (после 'на ...')
    """
    text = raw_text.lower()

    outcome_parts: list[str] = []
    event: str | None = None

    # ----- 1X2: П1 / П2 / Х / 1X / X2 / 12 -----
    if re.search(r"\bп1\b", text):
        outcome_parts.append("П1")
    if re.search(r"\bп2\b", text):
        outcome_parts.append("П2")
    if re.search(r"\b(х|ничья)\b", text):
        outcome_parts.append("Х")
    if re.search(r"\b1x\b", text):
        outcome_parts.append("1X")
    if re.search(r"\bx2\b", text):
        outcome_parts.append("X2")
    if re.search(r"\b12\b", text):
        outcome_parts.append("12")
    if re.search(r"\b1х\b", text):
        outcome_parts.append("1X")
    if re.search(r"\bх2\b", text):
        outcome_parts.append("X2")

    # ----- Тоталы -----
    m_total = re.search(
        r"тотал\s+(больше|меньше)\s*(\d+([\.,]\d+)?)",
        text,
    )
    if m_total:
        sign = m_total.group(1)
        line = m_total.group(2)
        prefix = "ТБ" if "больше" in sign else "ТМ"
        outcome_parts.append(f"{prefix} {line.replace('.', ',')}")

    # ТБ / ТМ сокращённо
    m_tb_tm = re.search(
        r"\bт(б|м)\s*(\d+([\.,]\d+)?)",
        text,
    )
    if m_tb_tm:
        letter = m_tb_tm.group(1)
        line = m_tb_tm.group(2)
        prefix = "ТБ" if letter == "б" else "ТМ"
        outcome_parts.append(f"{prefix} {line.replace('.', ',')}")

    # ----- Форы -----
    m_fora_short = re.search(
        r"\bф(1|2)\s*\(?\s*([+-]?\d+([\.,]\d+)?)\s*\)?",
        text,
    )
    if m_fora_short:
        side = m_fora_short.group(1)
        val = m_fora_short.group(2)
        outcome_parts.append(f"Ф{side}({val.replace('.', ',')})")

    m_fora_long = re.search(
        r"фора\s*(1|2)?\s*([+-]?\d+([\.,]\d+)?)",
        text,
    )
    if m_fora_long:
        side = m_fora_long.group(1)
        val = m_fora_long.group(2)
        if side:
            outcome_parts.append(f"Ф{side}({val.replace('.', ',')})")
        else:
            outcome_parts.append(f"Ф({val.replace('.', ',')})")

    outcome = " ; ".join(dict.fromkeys(outcome_parts)) if outcome_parts else None

    # ----- EVENT: текст после "на ..." -----
    lower = text
    idx = lower.find(" на ")
    if idx != -1:
        after = raw_text[idx + 4 :]  # всё после " на "
        cut_keywords = [
            " тотал",
            " фора",
            " по ",
            " за ",
            " коэф",
            " коэффициент",
            " кф ",
            " кэф",
        ]
        end_pos = len(after)
        after_lower = after.lower()
        for kw in cut_keywords:
            pos = after_lower.find(kw)
            if pos != -1:
                end_pos = min(end_pos, pos)
        event_candidate = after[:end_pos].strip(" -–—,:;")
        if event_candidate:
            event = event_candidate

    return outcome, event


def _extract_first_number(text: str) -> float | None:
    """
    Достаём первое число из строки (для банка, пополнения и т.п.).
    """
    m = re.search(r"(\d+([\.,]\d+)?)", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


# ------------------ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ПО СТАВКАМ ------------------


def _get_last_7d_bets(session: Session, user_id: int):
    """
    Достаём все ставки пользователя за последние 7 дней.
    Возвращаем: (ставки, начало периода, конец периода).
    """
    from .bets_db import Bet  # локальный импорт, чтобы не плодить циклы

    now = datetime.utcnow()
    period_start = now - timedelta(days=7)
    bets = session.exec(
        select(Bet).where(
            Bet.user_id == user_id,
            Bet.created_at >= period_start,
        )
    ).all()
    return bets, period_start, now


# ------------------ ОТЧЁТ ЗА НЕДЕЛЮ ------------------


def build_weekly_report(session: Session, user_id: int) -> str:
    """
    Строим отчёт за последние 7 дней по ставкам пользователя.
    """
    bets, period_start, now = _get_last_7d_bets(session, user_id)

    if not bets:
        return (
            "За последние 7 дней у тебя не было записанных ставок. "
            "Сделай пару ставок, и я смогу собрать для тебя отчёт 😉"
        )

    settled = [b for b in bets if b.result in ("win", "lose")]
    pushes = [b for b in bets if b.result == "push"]
    wins = [b for b in bets if b.result == "win"]

    total_bets = len(bets)
    settled_count = len(settled)
    pushes_count = len(pushes)

    total_stake = sum(b.stake or 0 for b in bets if b.stake is not None)
    total_pnl = sum(b.profit or 0 for b in bets if b.profit is not None)

    winrate = (
        (len(wins) / settled_count * 100.0) if settled_count > 0 else None
    )
    roi = (total_pnl / total_stake * 100.0) if total_stake > 0 else None

    bets_with_profit = [b for b in bets if b.profit is not None]
    best_bet = (
        max(bets_with_profit, key=lambda b: b.profit)
        if bets_with_profit
        else None
    )
    worst_bet = (
        min(bets_with_profit, key=lambda b: b.profit)
        if bets_with_profit
        else None
    )

    period_str = f"{period_start:%d.%m}–{now:%d.%m}"

    lines: list[str] = []
    lines.append(f"📈 Отчёт за последние 7 дней ({period_str}):")
    lines.append(f"Всего ставок: {total_bets}")

    if settled_count > 0:
        lines.append(f"Рассчитано (win/lose): {settled_count}")
    if pushes_count > 0:
        lines.append(f"Возвратов: {pushes_count}")

    if winrate is not None:
        lines.append(f"Винрейт: {winrate:.1f}%")
    if roi is not None:
        lines.append(f"ROI: {roi:.2f}%")
    if total_pnl:
        sign = "+" if total_pnl >= 0 else ""
        lines.append(f"PnL за период: {sign}{total_pnl:.0f}")
    if total_stake:
        lines.append(f"Общий объём ставок: {total_stake:.0f}")

    if best_bet is not None and worst_bet is not None and best_bet != worst_bet:
        lines.append("")
        lines.append("🏆 Лучшая ставка недели:")
        desc_best = best_bet.raw_text or ""
        sign_best = "+" if (best_bet.profit or 0) >= 0 else ""
        pnl_best = f"{sign_best}{(best_bet.profit or 0):.0f}"
        lines.append(f"• {desc_best}")
        lines.append(f"• Результат: {pnl_best}")

        lines.append("")
        lines.append("⚠️ Самая слабая ставка недели:")
        desc_worst = worst_bet.raw_text or ""
        sign_worst = "+" if (worst_bet.profit or 0) >= 0 else ""
        pnl_worst = f"{sign_worst}{(worst_bet.profit or 0):.0f}"
        lines.append(f"• {desc_worst}")
        lines.append(f"• Результат: {pnl_worst}")

    lines.append("")
    lines.append(
        "Подсказка: чтобы улучшать игру, смотри, какие рынки и типы ставок тянут PnL вниз. "
        "Позже я дам по ним отдельные рекомендации."
    )

    return "\n".join(lines)


# ------------------ ЛУЧШАЯ СТАВКА НЕДЕЛИ ------------------


def build_best_bet_insight(session: Session, user_id: int) -> str:
    """
    Коучинговый инсайт: лучшая ставка за 7 дней.
    """
    bets, period_start, now = _get_last_7d_bets(session, user_id)
    bets_with_profit = [b for b in bets if b.profit is not None]

    if not bets:
        return (
            "За последние 7 дней у тебя ещё не было ставок. "
            "Как только появятся победные, я покажу лучшую ставку недели."
        )

    if not bets_with_profit:
        return (
            "За последние 7 дней ставки либо ещё не рассчитаны, либо пока все без результата.\n"
            "Когда появятся рассчитанные ставки, я смогу выделить лучшую."
        )

    best_bet = max(bets_with_profit, key=lambda b: b.profit or 0)
    period_str = f"{period_start:%d.%m}–{now:%d.%m}"

    sign_best = "+" if (best_bet.profit or 0) >= 0 else ""
    pnl_best = f"{sign_best}{(best_bet.profit or 0):.0f}"

    lines: list[str] = []
    lines.append(f"🏆 Лучшая ставка недели ({period_str}):")
    if best_bet.created_at:
        lines.append(f"Дата: {best_bet.created_at:%d.%m %H:%M}")
    if best_bet.raw_text:
        lines.append(f"Ставка: {best_bet.raw_text}")
    else:
        parts = []
        if best_bet.event:
            parts.append(best_bet.event)
        if best_bet.outcome:
            parts.append(best_bet.outcome)
        if best_bet.stake is not None:
            parts.append(f"сумма: {best_bet.stake:g}")
        if best_bet.odds is not None:
            parts.append(f"кэф: {best_bet.odds:.2f}")
        if parts:
            lines.append("Ставка: " + " | ".join(parts))

    lines.append(f"Результат по прибыли: {pnl_best}")

    lines.append("")
    lines.append(
        "Идея: посмотри, что именно ты увидел в этом матче/рынке. "
        "Такие решения стоит искать и повторять в будущем."
    )

    return "\n".join(lines)


# ------------------ ОШИБКА НЕДЕЛИ ------------------


def build_worst_bet_insight(session: Session, user_id: int) -> str:
    """
    Инсайт: самая слабая ставка за 7 дней (ошибка недели).
    """
    bets, period_start, now = _get_last_7d_bets(session, user_id)
    bets_with_profit = [b for b in bets if b.profit is not None]

    if not bets:
        return (
            "За последние 7 дней у тебя ещё не было ставок. "
            "Когда появятся сделки, я смогу подсветить ошибку недели."
        )

    if not bets_with_profit:
        return (
            "За последние 7 дней ставки либо ещё не рассчитаны, либо пока без результата.\n"
            "Когда появятся рассчитанные ставки, я смогу выделить самую слабую."
        )

    worst_bet = min(bets_with_profit, key=lambda b: b.profit or 0)
    period_str = f"{period_start:%d.%m}–{now:%d.%m}"

    sign_worst = "+" if (worst_bet.profit or 0) >= 0 else ""
    pnl_worst = f"{sign_worst}{(worst_bet.profit or 0):.0f}"

    lines: list[str] = []
    lines.append(f"⚠️ Ошибка недели ({period_str}):")
    if worst_bet.created_at:
        lines.append(f"Дата: {worst_bet.created_at:%d.%m %H:%M}")
    if worst_bet.raw_text:
        lines.append(f"Ставка: {worst_bet.raw_text}")
    else:
        parts = []
        if worst_bet.event:
            parts.append(worst_bet.event)
        if worst_bet.outcome:
            parts.append(worst_bet.outcome)
        if worst_bet.stake is not None:
            parts.append(f"сумма: {worst_bet.stake:g}")
        if worst_bet.odds is not None:
            parts.append(f"кэф: {worst_bet.odds:.2f}")
        if parts:
            lines.append("Ставка: " + " | ".join(parts))

    lines.append(f"Результат по прибыли: {pnl_worst}")

    lines.append("")
    lines.append(
        "Важно не просто зафиксировать минус, а понять причину:\n"
        "• переоценил команду?\n"
        "• зашёл в неудобный рынок?\n"
        "• заиграл слишком большой размер ставки?\n"
        "Такие моменты — лучший материал для роста."
    )

    return "\n".join(lines)


# ------------------ КАТЕГОРИЯ РЫНКА ------------------


def _get_market_category(outcome: str | None) -> str:
    """
    Грубое определение типа рынка по строке исхода.
    """
    out = (outcome or "").upper()
    if any(k in out for k in ("ТБ", "ТМ", "ТОТАЛ")):
        return "тоталы"
    if "Ф" in out:
        return "форы"
    if any(k in out for k in ("П1", "П2", " Х", "1X", "Х2", "X2", "12")):
        return "1X2"
    return "другое"


# ------------------ АНАЛИТИКА РЫНКОВ ПОЛЬЗОВАТЕЛЯ ------------------


def build_user_market_insights(session: Session, user_id: int) -> str:
    """
    Анализ по типам рынков (1X2, тоталы, форы, другое) по ВСЕМ ставкам пользователя.
    """
    bets = get_all_bets(session, user_id)

    if not bets:
        return (
            "У тебя пока нет сохранённых ставок. "
            "Как только появится история, я покажу, какие рынки заходят лучше всего."
        )

    settled = [b for b in bets if b.result in ("win", "lose")]
    if not settled:
        return (
            "У тебя есть сохранённые ставки, но ещё ни одна не рассчитана.\n"
            "Когда появятся win/lose, я смогу разобрать твои рынки."
        )

    stats_by_cat: dict[str, dict[str, float]] = {}

    for b in settled:
        cat = _get_market_category(b.outcome)
        d = stats_by_cat.setdefault(
            cat,
            {
                "bets": 0,
                "wins": 0,
                "losses": 0,
                "stake": 0.0,
                "pnl": 0.0,
            },
        )
        d["bets"] += 1
        if b.result == "win":
            d["wins"] += 1
        elif b.result == "lose":
            d["losses"] += 1
        if b.stake is not None:
            d["stake"] += float(b.stake)
        if b.profit is not None:
            d["pnl"] += float(b.profit)

    for cat, d in stats_by_cat.items():
        if d["bets"] > 0:
            d["winrate"] = d["wins"] / d["bets"] * 100.0
        else:
            d["winrate"] = None
        if d["stake"] > 0:
            d["roi"] = d["pnl"] / d["stake"] * 100.0
        else:
            d["roi"] = None

    cats_with_sample = [
        (cat, d) for cat, d in stats_by_cat.items() if d["bets"] >= 3
    ]
    best_cat = None
    worst_cat = None
    if cats_with_sample:
        best_cat = max(
            cats_with_sample,
            key=lambda x: x[1].get("roi", float("-inf")),
        )
        worst_cat = min(
            cats_with_sample,
            key=lambda x: x[1].get("roi", float("inf")),
        )

    lines: list[str] = []
    lines.append("📊 Разбор по типам рынков (за всё время):")
    for cat, d in stats_by_cat.items():
        line = f"• {cat}: ставок {int(d['bets'])}"
        if d.get("winrate") is not None:
            line += f", winrate {d['winrate']:.1f}%"
        if d.get("roi") is not None:
            line += f", ROI {d['roi']:.2f}%"
        if d["pnl"]:
            sign = "+" if d["pnl"] >= 0 else ""
            line += f", PnL {sign}{d['pnl']:.0f}"
        lines.append(line)

    lines.append("")

    if best_cat and worst_cat and best_cat[0] != worst_cat[0]:
        bcat, bd = best_cat
        wcat, wd = worst_cat
        lines.append("✅ Твой самый сильный рынок:")
        lines.append(
            f"• {bcat}: winrate {bd['winrate']:.1f}%, ROI {bd['roi']:.2f}% "
            f"на выборке {int(bd['bets'])} ставок."
        )
        lines.append("")
        lines.append("⚠️ Рынок, который тянет вниз:")
        lines.append(
            f"• {wcat}: winrate {wd['winrate']:.1f}%, ROI {wd['roi']:.2f}% "
            f"на выборке {int(wd['bets'])} ставок."
        )
        lines.append("")
        lines.append(
            "Идея: усиливай игру на сильных рынках и аккуратно относись к слабым — "
            "там можно снижать размер ставки или вводить дополнительные фильтры."
        )
    else:
        lines.append(
            "Пока выборка по рынкам небольшая или распределена неявно.\n"
            "Когда набросаешь больше ставок, я смогу точнее подсветить сильные и слабые зоны."
        )

    return "\n".join(lines)


# ------------------ БАНК И РЕКОМЕНДАЦИИ ПО РАЗМЕРУ СТАВКИ ------------------


def _calc_recommended_stake_range(bank: float) -> tuple[float, float, float, float]:
    """
    Возвращает (low_pct, high_pct, low_amt, high_amt).
    Простая модель:
    - банк ≤ 30k → 1–2%
    - 30k–100k  → 1–3%
    - 100k–300k → 1–4%
    - >300k     → 1–5%
    """
    if bank <= 30_000:
        low_pct, high_pct = 0.01, 0.02
    elif bank <= 100_000:
        low_pct, high_pct = 0.01, 0.03
    elif bank <= 300_000:
        low_pct, high_pct = 0.01, 0.04
    else:
        low_pct, high_pct = 0.01, 0.05

    low_amt = bank * low_pct
    high_amt = bank * high_pct
    return low_pct, high_pct, low_amt, high_amt


def _build_bank_status_text(bank: float) -> str:
    low_pct, high_pct, low_amt, high_amt = _calc_recommended_stake_range(bank)
    lines: list[str] = []
    lines.append("💰 Твой банк:")
    lines.append(f"Текущий банк: {bank:.0f}")
    lines.append("")
    lines.append(
        f"Базовый консервативный диапазон ставки: {low_amt:.0f}–{high_amt:.0f} "
        f"({int(low_pct*100)}–{int(high_pct*100)}% от банка)."
    )
    lines.append(
        "Это не совет 'ставь так', а ориентир по риск-менеджменту, чтобы не улетать в просадку."
    )
    return "\n".join(lines)


def _build_bank_hint_for_stake(bank: float, stake: float | None) -> list[str]:
    """
    Строим подсказку по размеру ставки относительно банка.
    Возвращаем список строк, которые можно добавить к ответу.
    """
    if bank <= 0:
        return []

    low_pct, high_pct, low_amt, high_amt = _calc_recommended_stake_range(bank)
    lines: list[str] = []
    lines.append("")
    lines.append("💡 Банк-менеджмент:")
    lines.append(f"Текущий банк: {bank:.0f}")
    lines.append(
        f"Рекомендованный размер ставки: {low_amt:.0f}–{high_amt:.0f} "
        f"({int(low_pct*100)}–{int(high_pct*100)}% от банка)."
    )

    if stake is not None and stake > 0:
        if stake < low_amt * 0.7:
            lines.append(
                f"Ты ставишь заметно ниже диапазона (ставка {stake:.0f}). Это очень аккуратно."
            )
        elif stake > high_amt * 1.3:
            lines.append(
                f"Ты ставишь заметно выше диапазона (ставка {stake:.0f}). Это агрессивный риск."
            )
        else:
            lines.append(
                f"Текущий размер ставки ({stake:.0f}) примерно в разумном диапазоне для этого банка."
            )

    return lines


# ------------------ ОЦЕНКА КОНКРЕТНОЙ СТАВКИ ------------------


def build_stake_evaluation(session: Session, user_id: int, raw_text: str) -> str:
    """
    Разбираем ставку из текста, смотрим:
    - банк и размер ставки относительно него;
    - исторический результат пользователя по этому типу рынка.
    Даём чек-лист, без 'ставь / не ставь'.
    """
    stake, odds = _parse_stake_and_odds(raw_text)
    outcome, event = _parse_outcome_and_event(raw_text)

    lines: list[str] = []
    lines.append("🔎 Предварительная оценка ставки (чек-лист, а не совет):")
    text_clean = raw_text.strip()
    if text_clean:
        lines.append(f"Текст: {text_clean}")

    if event or outcome or stake is not None or odds is not None:
        lines.append("")
        lines.append("Разбор формата ставки:")
        if event:
            lines.append(f"• Событие: {event}")
        if outcome:
            lines.append(f"• Исход: {outcome}")
        if stake is not None:
            lines.append(f"• Сумма: {stake:g}")
        if odds is not None:
            lines.append(f"• Коэффициент: {odds:.2f}")

    bank = get_user_bank(session, user_id)
    if bank is not None:
        lines.extend(_build_bank_hint_for_stake(bank, stake))
    else:
        lines.append("")
        lines.append(
            "Банк пока не задан, поэтому не могу оценить риск относительно твоего банка.\n"
            "Можешь задать его командой 'мой банк 100000'."
        )

    market_cat = _get_market_category(outcome)
    bets = get_all_bets(session, user_id)
    settled = [b for b in bets if b.result in ("win", "lose")]
    cat_bets = [b for b in settled if _get_market_category(b.outcome) == market_cat]

    lines.append("")
    lines.append(f"📊 Твой опыт на рынке: {market_cat}")
    if not cat_bets:
        lines.append(
            "По этому типу рынка у тебя пока нет рассчитанных ставок.\n"
            "Решение полностью опирается на твой анализ матча и отношение к риску."
        )
        lines.append("")
        lines.append(
            "Итог: я не говорю 'ставь/не ставь', а подсвечиваю структуру решения. "
            "Смотри, не выбивается ли размер ставки из адекватного процента от банка."
        )
        return "\n".join(lines)

    n = len(cat_bets)
    wins = [b for b in cat_bets if b.result == "win"]
    stake_sum = sum(b.stake or 0.0 for b in cat_bets)
    pnl_sum = sum(b.profit or 0.0 for b in cat_bets)
    winrate = (len(wins) / n * 100.0) if n > 0 else None
    roi = (pnl_sum / stake_sum * 100.0) if stake_sum > 0 else None

    lines.append(f"• Ставок на этом рынке: {n}")
    if winrate is not None:
        lines.append(f"• Winrate: {winrate:.1f}%")
    if roi is not None:
        lines.append(f"• ROI: {roi:.2f}%")
    if pnl_sum:
        sign = "+" if pnl_sum >= 0 else ""
        lines.append(f"• Совокупный PnL: {sign}{pnl_sum:.0f}")

    if n < 5:
        lines.append(
            "Выборка по этому рынку пока маленькая, выводы по статистике — очень осторожные."
        )
    else:
        if roi is not None and roi > 0:
            lines.append(
                "Этот рынок для тебя пока выглядит сильным по истории — ты в нём зарабатываешь."
            )
        elif roi is not None and roi < 0:
            lines.append(
                "По истории этот рынок тянет тебя в минус. "
                "Можно играть аккуратнее: уменьшать % от банка или фильтровать такие ситуации."
            )
        else:
            lines.append(
                "По истории этот рынок пока около нуля. Важнее качество конкретной идеи по матчу."
            )

    lines.append("")
    lines.append(
        "Итог: решать всё равно тебе. Я даю контекст — риск относительно банка и твой опыт "
        "по этому рынку, а не команду 'ставь/не ставь'."
    )

    return "\n".join(lines)


# ------------------ ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ ------------------


def build_user_profile(session: Session, user_id: int) -> str:
    """
    Личный кабинет / профиль игрока:
    - банк
    - общая статистика
    - результат за 7 дней
    - сильный и слабый рынок
    - персональный совет
    """
    stats = get_user_stats(session, user_id)
    bank = get_user_bank(session, user_id)
    bets = get_all_bets(session, user_id)

    lines: list[str] = []
    lines.append("👤 Твой профиль игрока:")

    # Блок банка
    lines.append("")
    if bank is not None:
        lines.append(_build_bank_status_text(bank))
    else:
        lines.append(
            "💰 Банк ещё не задан.\n"
            "Задай, например: 'мой банк 100000', и я начну отслеживать риск и просадку."
        )

    # Общая статистика
    lines.append("")
    lines.append("📊 Общая статистика:")
    if stats.total_bets == 0:
        lines.append("Пока нет ни одной сохранённой ставки.")
    else:
        lines.append(f"Всего ставок: {stats.total_bets}")
        lines.append(f"Рассчитано (win/lose): {stats.settled_bets}")
        if stats.pushes:
            lines.append(f"Возвратов: {stats.pushes}")
        if stats.settled_bets > 0:
            lines.append(f"Winrate: {stats.winrate:.1f}%")
            lines.append(f"ROI: {stats.roi:.2f}%")
            sign_pnl = "+" if stats.pnl >= 0 else ""
            lines.append(f"PnL за всё время: {sign_pnl}{stats.pnl:.0f}")
            lines.append(f"Общий объём ставок: {stats.total_stake:.0f}")

    # Результат за 7 дней
    bets_7d, period_start, now = _get_last_7d_bets(session, user_id)
    lines.append("")
    lines.append("⏱ Результат за последние 7 дней:")
    if not bets_7d:
        lines.append(
            "За последние 7 дней у тебя не было записанных ставок."
        )
    else:
        settled_7d = [b for b in bets_7d if b.result in ("win", "lose")]
        pnl_7d = sum(b.profit or 0.0 for b in settled_7d)
        sign_7d = "+" if pnl_7d >= 0 else ""
        lines.append(
            f"Период: {period_start:%d.%m}–{now:%d.%m}, PnL: {sign_7d}{pnl_7d:.0f}"
        )
        if settled_7d:
            wins_7d = [b for b in settled_7d if b.result == "win"]
            winrate_7d = len(wins_7d) / len(settled_7d) * 100.0
            lines.append(f"Winrate за период: {winrate_7d:.1f}%")

    # Сильный / слабый рынок
    lines.append("")
    lines.append("🎯 Твои рынки:")

    settled_all = [b for b in bets if b.result in ("win", "lose")]
    best_cat = None
    worst_cat = None
    if not settled_all:
        lines.append(
            "Пока рано делить на сильные и слабые рынки — нет рассчитанных ставок."
        )
    else:
        stats_by_cat: dict[str, dict[str, float]] = {}
        for b in settled_all:
            cat = _get_market_category(b.outcome)
            d = stats_by_cat.setdefault(
                cat,
                {
                    "bets": 0,
                    "wins": 0,
                    "losses": 0,
                    "stake": 0.0,
                    "pnl": 0.0,
                },
            )
            d["bets"] += 1
            if b.result == "win":
                d["wins"] += 1
            elif b.result == "lose":
                d["losses"] += 1
            if b.stake is not None:
                d["stake"] += float(b.stake)
            if b.profit is not None:
                d["pnl"] += float(b.profit)

        for cat, d in stats_by_cat.items():
            if d["bets"] > 0:
                d["winrate"] = d["wins"] / d["bets"] * 100.0
            else:
                d["winrate"] = None
            if d["stake"] > 0:
                d["roi"] = d["pnl"] / d["stake"] * 100.0
            else:
                d["roi"] = None

        cats_with_sample = [
            (cat, d) for cat, d in stats_by_cat.items() if d["bets"] >= 3
        ]
        if cats_with_sample:
            best_cat = max(
                cats_with_sample,
                key=lambda x: x[1].get("roi", float("-inf")),
            )
            worst_cat = min(
                cats_with_sample,
                key=lambda x: x[1].get("roi", float("inf")),
            )

        if best_cat:
            cat, d = best_cat
            lines.append(
                f"✅ Сильный рынок: {cat} "
                f"(winrate {d['winrate']:.1f}%, ROI {d['roi']:.2f}%, "
                f"ставок {int(d['bets'])})"
            )
        if worst_cat and (not best_cat or worst_cat[0] != best_cat[0]):
            cat, d = worst_cat
            lines.append(
                f"⚠️ Слабый рынок: {cat} "
                f"(winrate {d['winrate']:.1f}%, ROI {d['roi']:.2f}%, "
                f"ставок {int(d['bets'])})"
            )
        if not cats_with_sample:
            lines.append(
                "Выборка по рынкам пока небольшая. Чем больше сыграешь, тем точнее я покажу сильные и слабые зоны."
            )

    # Персональный совет
    lines.append("")
    lines.append("🧠 Совет по твоей игре:")

    advice_parts: list[str] = []

    # по 7д результату
    if bets_7d and settled_all:
        pnl_7d = sum(
            b.profit or 0.0
            for b in [b for b in bets_7d if b.result in ("win", "lose")]
        )
        if pnl_7d < 0:
            advice_parts.append(
                "Последние 7 дней у тебя в лёгком минусе. Логично немного снизить средний % ставки от банка."
            )
        elif pnl_7d > 0:
            advice_parts.append(
                "Последние 7 дней у тебя в плюсе — важно не завышать ставки из-за серии успехов."
            )

    # по банку
    if bank is None:
        advice_parts.append(
            "Сейчас ты играешь без фиксированного банка. Чтобы контролировать риск, "
            "установи банк и держись безопасного процента от него (обычно 1–5%)."
        )


    # по рынкам
    if settled_all and best_cat:
        cat, d = best_cat
        advice_parts.append(
            f"Продолжай мониторить ситуаций на рынке '{cat}' — по истории он для тебя самый сильный. "
            "Туда можно ставить базовый % от банка."
        )
    if settled_all and worst_cat and (not best_cat or worst_cat[0] != best_cat[0]):
        cat, d = worst_cat
        advice_parts.append(
            f"На рынке '{cat}' пока осторожнее: по истории он тянет вниз. "
            "Хорошая идея — уменьшить там % от банка или ставить только в самых очевидных спотах."
        )

    if not advice_parts:
        advice_parts.append(
            "Истории пока мало, чтобы дать точный совет. Главное — держать размер ставки в адекватном "
            "проценте от банка и не догоняться."
        )

    for p in advice_parts:
        lines.append(f"• {p}")

    lines.append("")
    lines.append(
        "Для деталей можешь спросить:\n"
        "• 'разбор моих рынков'\n"
        "• 'отчёт за неделю'\n"
        "• 'оценка ставки 1000 на СКА тотал больше 5.5 за 1.9'"
    )

    return "\n".join(lines)


# ------------------ АНАЛИТИКА МАТЧА КХЛ ПО ID ------------------


def build_khl_match_analysis(event) -> str:
    """
    Разбор матча КХЛ по объекту event из khl_client.

    Что делаем:
    - разбираем линию 1X2 (фаворит / андердог, имплайд-вероятности, маржа)
    - по возможности показываем рынок тотала (основная линия)
    - добавляем блок "Форма команд" (через khl_form_client)
    - добавляем лёгкий value-чек-лист (без прямых советов 'ставь/не ставь')
    """
    team1 = getattr(event, "team1", "?")
    team2 = getattr(event, "team2", "?")
    event_id = getattr(event, "id", "?")
    title = f"{team1} — {team2} (id: {event_id})"

    markets = getattr(event, "markets", []) or []

    # --- 1) Поиск рынков 1X2 и тотала ---
    market_1x2 = None
    market_total = None

    for m in markets:
        name = (getattr(m, "name", "") or "").upper()
        if market_1x2 is None and name in ("1X2", "1X", "3WAY", "3-WAY"):
            market_1x2 = m
        if market_total is None and any(k in name for k in ("TOTAL", "ТОТАЛ", "TOTALS", "OVER/UNDER")):
            market_total = m

    lines: list[str] = []
    lines.append("📊 Разбор матча КХЛ:")
    lines.append(title)
    lines.append("")

    # --- 2) Если нет линии вообще ---
    if not markets:
        lines.append(
            "По этому матчу я не вижу доступных рынков в линии. "
            "Возможно, матч ещё не открыт у букмекера или данные не подгрузились."
        )
        return "\n".join(lines)

    # чтобы позже использовать в value-блоке
    fav_name = dog_name = None
    fav_coef = dog_coef = None

    # --- 3) Разбор рынка 1X2 ---
    if market_1x2:
        outcomes = getattr(market_1x2, "outcomes", []) or []
        odds_list: list[tuple[str, float]] = []

        for o in outcomes:
            name = str(getattr(o, "name", "?"))
            price = getattr(o, "price", None)
            try:
                price_f = float(price)
            except (TypeError, ValueError):
                continue
            if price_f < 1.01:
                continue
            odds_list.append((name, price_f))

        if odds_list:
            lines.append("Линия 1X2 (коэффициенты и имплайд-вероятности):")

            implied = [(name, 100.0 / coef) for name, coef in odds_list]
            sum_implied = sum(p for _, p in implied)
            margin = sum_implied - 100.0 if sum_implied > 0 else 0.0

            for (name, coef), (_, p_imp) in zip(odds_list, implied):
                lines.append(f"• {name}: кэф {coef:.2f}, импл. вероятность ≈ {p_imp:.1f}%")

            if margin:
                lines.append("")
                lines.append(f"Маржа букмекера по рынку 1X2 ≈ {margin:.1f} п.п.")

            # Оценка "честных" вероятностей (без маржи)
            if sum_implied > 0:
                lines.append("")
                lines.append("Оценка 'честных' вероятностей (без маржи бука):")
                for (name, _), (_, p_imp) in zip(odds_list, implied):
                    fair = p_imp * 100.0 / sum_implied
                    lines.append(f"• {name}: ≈ {fair:.1f}%")

            # Определяем фаворита / андердога
            fav_name, fav_coef = min(odds_list, key=lambda x: x[1])
            dog_name, dog_coef = max(odds_list, key=lambda x: x[1])

            lines.append("")
            lines.append("Структура матча по 1X2:")

            ratio = dog_coef / fav_coef if (fav_coef and fav_coef > 0) else None
            if ratio is not None:
                if ratio < 1.4:
                    lines.append(
                        "• Линия достаточно ровная — ожидается более-менее равный матч без явного суперфаворита."
                    )
                elif ratio < 2.2:
                    lines.append(
                        f"• {fav_name} идёт фаворитом, но андердог ({dog_name}) не выглядит безнадёжным по линии."
                    )
                else:
                    lines.append(
                        f"• {fav_name} — явный фаворит по линии, {dog_name} играет роль заметного андердога."
                    )
            else:
                lines.append("• Фаворит и андердог по линии определяются, но коэффициенты выглядят странно.")

            lines.append(
                "• Помни, что линия отражает оценку букмекера и рынка, а не гарантию результата."
            )
        else:
            lines.append(
                "По рынку 1X2 я не нашёл валидных коэффициентов. "
                "Возможно, матч в лайве или линия временно снята."
            )
    else:
        lines.append(
            "По этому матчу я не вижу классического рынка 1X2. "
            "Скорее всего, доступны только альтернативные рынки или линия урезана."
        )

    # --- 4) Разбор тотала, если есть ---
    main_total_line: float | None = None
    over = under = None

    if market_total:
        outcomes = getattr(market_total, "outcomes", []) or []

        for o in outcomes:
            name_raw = str(getattr(o, "name", "") or "")
            name_up = name_raw.upper()
            price = getattr(o, "price", None)
            try:
                price_f = float(price)
            except (TypeError, ValueError):
                continue
            if price_f < 1.01:
                continue

            if any(k in name_up for k in ("OVER", "ТБ")):
                over = (name_raw, price_f)
            elif any(k in name_up for k in ("UNDER", "ТМ")):
                under = (name_raw, price_f)

        if over or under:
            lines.append("")
            lines.append("Рынок тотала (основная линия, если удалось определить):")

            if over:
                lines.append(f"• {over[0]}: кэф {over[1]:.2f}")
            if under:
                lines.append(f"• {under[0]}: кэф {under[1]:.2f}")

            # Попробуем выдернуть саму линию тотала из названия (например, 'ТБ 5.5')
            def _extract_total_line(name: str) -> float | None:
                m = re.search(r"(\d+([\.,]\d+)?)", name)
                if not m:
                    return None
                try:
                    return float(m.group(1).replace(",", "."))
                except ValueError:
                    return None

            if over and not main_total_line:
                main_total_line = _extract_total_line(over[0])
            if under and not main_total_line:
                main_total_line = _extract_total_line(under[0])

            if over and under:
                p_over = 100.0 / over[1]
                p_under = 100.0 / under[1]
                sum_p = p_over + p_under
                margin_tot = sum_p - 100.0

                lines.append("")
                lines.append(
                    f"Импл. вероятность: ТБ ≈ {p_over:.1f}%, ТМ ≈ {p_under:.1f}%. "
                    "Сумма выше 100% из-за маржи бука."
                )
                lines.append(f"Оценочная маржа по тоталу ≈ {margin_tot:.1f} п.п.")
        else:
            lines.append("")
            lines.append(
                "Рынок тотала присутствует, но не удалось однозначно выделить основную линию ТБ/ТМ."
            )

    # --- 5) Форма команд (через khl_form_client) ---
    lines.append("")
    lines.append("📉 Форма команд (по последним матчам):")

    form1 = get_team_form(team1)
    form2 = get_team_form(team2)

    avg_total_form: float | None = None

    if form1:
        lines.append(
            f"• {form1.team_name}: {form1.wins}-{form1.losses} за последние {form1.games} матчей, "
            f"забивают в среднем {form1.goals_for:.1f}, пропускают {form1.goals_against:.1f}, "
            f"средний тотал ≈ {form1.avg_total:.1f}."
        )
    else:
        lines.append(f"• {team1}: форму не удалось оценить (недостаточно данных).")

    if form2:
        lines.append(
            f"• {form2.team_name}: {form2.wins}-{form2.losses} за последние {form2.games} матчей, "
            f"забивают в среднем {form2.goals_for:.1f}, пропускают {form2.goals_against:.1f}, "
            f"средний тотал ≈ {form2.avg_total:.1f}."
        )
    else:
        lines.append(f"• {team2}: форму не удалось оценить (недостаточно данных).")

    if form1 and form2:
        avg_total_form = (form1.avg_total + form2.avg_total) / 2.0

    # --- 6) Лёгкий value-чек-лист ---
    lines.append("")
    lines.append("🧩 Value-чек-лист (подсказка, не прогноз):")

    # 6.1. Сравнение тотала линии и тотала по форме
    if main_total_line is not None and avg_total_form is not None:
        diff = avg_total_form - main_total_line
        lines.append(
            f"• Средний тотал по форме команд ≈ {avg_total_form:.1f} против линии тотала ≈ {main_total_line:.1f}."
        )
        if diff > 0.4:
            lines.append(
                "  Форма команд чуть более 'верховая', чем заложено в линии — повод присмотреться к ТБ, "
                "но обязательно учитывай контекст матча."
            )
        elif diff < -0.4:
            lines.append(
                "  Форма команд более низовая, чем ожидает линия — можно внимательнее посмотреть в сторону ТМ."
            )
        else:
            lines.append(
                "  Линия тотала в целом согласуется с тем, что показывают последние матчи команд."
            )
    else:
        lines.append(
            "• Недостаточно данных, чтобы сравнить линию тотала с формой (нет явной линии ТБ/ТМ или статистики по тоталам)."
        )

    # 6.2. Баланс сил по форме vs линия 1X2
    if fav_name and form1 and form2:
        # грубо: у кого выше процент побед в последних матчах
        winrate1 = form1.wins / form1.games * 100.0 if form1.games > 0 else None
        winrate2 = form2.wins / form2.games * 100.0 if form2.games > 0 else None

        if winrate1 is not None and winrate2 is not None:
            lines.append("")
            lines.append("• Сравнение формы и линии 1X2:")

            if fav_name.startswith("1"):
                fav_team_name = team1
                fav_wr = winrate1
                dog_team_name = team2
                dog_wr = winrate2
            elif fav_name.startswith("2"):
                fav_team_name = team2
                fav_wr = winrate2
                dog_team_name = team1
                dog_wr = winrate1
            else:
                fav_team_name = fav_name
                fav_wr = None
                dog_team_name = dog_name
                dog_wr = None

            if fav_wr is not None and dog_wr is not None:
                lines.append(
                    f"  Фаворит по линии: {fav_team_name} (по форму {fav_wr:.1f}% побед), "
                    f"андердог: {dog_team_name} ({dog_wr:.1f}% побед)."
                )
                if abs(fav_wr - dog_wr) < 7:
                    lines.append(
                        "  По форме команды не так сильно отличаются, как это может казаться по коэффициентам."
                    )
                elif fav_wr > dog_wr + 10:
                    lines.append(
                        "  По форме фаворит выглядит убедительнее андердога — линия в целом поддерживается статистикой."
                    )
            else:
                lines.append("  Форму по победам считать некорректно — не хватает данных.")
    else:
        lines.append(
            "• Недостаточно данных, чтобы сопоставить форму с фаворитом по линии (нет 1X2 или формы обеих команд)."
        )

    lines.append("")
    lines.append(
        "Используй этот разбор как чек-лист: линия, маржа, форма и тоталы. "
        "Финальное решение по ставке всегда за тобой."
    )

    return "\n".join(lines)


# ------------------ ЛОГИКА АГЕНТА ------------------


async def run_agent(user_id: int, message: str, session: Session) -> str:
    """
    Простейший if/else-агент.
    """
    original_text = message or ""
    text = original_text.lower().strip()

    # 0) ГЛАВНОЕ МЕНЮ / СТАРТ
    if text in {"/start", "start", "меню", "главное меню", "help", "/help"}:
        return (
            "Я хоккейный AI-помощник для ставок 🏒\n\n"
            "Что я умею уже сейчас:\n"
            "🧾 *Мои ставки*\n"
            "  • сохранять ставки по тексту\n"
            "  • показывать последние\n"
            "  • считать winrate, ROI, PnL\n\n"
            "💰 *Банк и размер ставки*\n"
            "  • 'мой банк 100000' — задать банк\n"
            "  • 'состояние банка' — показать банк и рекомендуемый % ставки\n"
            "  • 'пополнить банк 20000' / 'уменьшить банк 5000'\n"
            "  • при расчёте ставки win/lose банк обновляется автоматически\n\n"
            "📊 *Аналитика матчей*\n"
            "  • 'КХЛ сегодня' — матчи и линия 1X2\n"
            "  • 'анализ матча 123' — odds-разбор по id\n\n"
            "📈 *Отчёты и инсайты по тебе*\n"
            "  • 'отчёт за неделю' — сводка по последним 7 дням\n"
            "  • 'лучшая ставка недели'\n"
            "  • 'ошибка недели'\n"
            "  • 'разбор моих рынков' — где ты силён, а где льёшь\n\n"
            "👤 *Профиль*\n"
            "  • 'профиль' — банк, статистика, сильные/слабые рынки, совет\n\n"
            "🧠 *Оценка конкретной ставки*\n"
            "  • 'оценка ставки 1000 на СКА тотал больше 5.5 за 1.9'\n"
            "  • 'что скажешь про ставку 1000 на СКА по 1.9'\n"
        )

    # 1) ОТМЕТИТЬ РЕЗУЛЬТАТ СТАВКИ + АВТО-ОБНОВЛЕНИЕ БАНКА
    m_res = re.search(
        r"ставка\s+(\d+)\s+(выиграл[аи]?|проиграл[аи]?|выигрыш|проигрыш|возврат|refund|push|win|lose|loss)",
        text,
    )
    if m_res:
        bet_id = int(m_res.group(1))
        res_word = m_res.group(2)

        bet = settle_bet(session, user_id, bet_id, res_word)
        if bet is None:
            return f"Не нашёл ставку с id {bet_id} или не понял результат."

        if bet.result == "win":
            human_res = "выигрыш"
        elif bet.result == "lose":
            human_res = "проигрыш"
        elif bet.result == "push":
            human_res = "возврат"
        else:
            human_res = bet.result or "неизвестно"

        lines: list[str] = []
        lines.append(f"Отметил ставку {bet.id} как {human_res}.")
        if bet.profit is not None:
            sign = "+" if bet.profit >= 0 else ""
            lines.append(f"Результат по сумме: {sign}{bet.profit:.0f}.")

        old_bank = get_user_bank(session, user_id)
        if old_bank is not None and bet.profit is not None:
            user = change_user_bank(session, user_id, bet.profit)
            if user.bank is not None:
                lines.append(f"Обновлённый банк: {user.bank:.0f}.")
        elif old_bank is None:
            lines.append(
                "Банк пока не задан, поэтому я его не меняю. "
                "Можешь задать его командой 'мой банк 100000'."
            )

        lines.append("")
        lines.append(
            "Посмотреть статистику: 'Покажи мою статистику', 'профиль' или 'отчёт за неделю'."
        )
        return "\n".join(lines)

    # 2) ПРОФИЛЬ
    if (
        text == "профиль"
        or "мой профиль" in text
        or "личный кабинет" in text
        or "мой аккаунт" in text
    ):
        return build_user_profile(session, user_id)

    # 3) КОМАНДЫ БАНКА

    if (
        "состояние банка" in text
        or text.strip() == "мой банк"
        or (("банк" in text) and ("баланс" in text))
    ):
        bank = get_user_bank(session, user_id)
        if bank is None:
            return (
                "Ты ещё не задал размер банка.\n"
                "Например: 'мой банк 100000'."
            )
        return _build_bank_status_text(bank)

    m_set_bank = re.search(
        r"(мой\s+банк|установи\s+банк|банк)\s+(\d+([\.,]\d+)?)",
        text,
    )
    if m_set_bank:
        value_str = m_set_bank.group(2)
        try:
            bank_val = float(value_str.replace(",", "."))
        except ValueError:
            bank_val = None

        if bank_val is None or bank_val <= 0:
            return "Не понял сумму банка. Укажи положительное число, например: 'мой банк 100000'."

        user = set_user_bank(session, user_id, bank_val)
        return (
            f"Банк установлен: {user.bank:.0f}.\n\n"
            + _build_bank_status_text(user.bank)
        )

    if "банк" in text and any(k in text for k in ("попол", "добав", "увелич")):
        delta = _extract_first_number(text)
        if delta is None or delta <= 0:
            return "Не понял, на какую сумму пополнить банк. Пример: 'пополнить банк 20000'."
        old_bank = get_user_bank(session, user_id)
        user = change_user_bank(session, user_id, delta)
        msg = f"Банк увеличен на {delta:.0f}. Текущий банк: {user.bank:.0f}."
        if old_bank is None:
            msg += "\n(Раньше банк не был задан, считаю, что ты стартовал с 0.)"
        msg += "\n\n" + _build_bank_status_text(user.bank)
        return msg

    if "банк" in text and any(k in text for k in ("уменьш", "сниз", "убав", "минус")):
        delta = _extract_first_number(text)
        if delta is None or delta <= 0:
            return "Не понял, на какую сумму уменьшить банк. Пример: 'уменьшить банк 5000'."
        delta = -abs(delta)
        user = change_user_bank(session, user_id, delta)
        return (
            f"Банк уменьшен на {abs(delta):.0f}. Текущий банк: {user.bank:.0f}.\n\n"
            + _build_bank_status_text(user.bank)
        )

    # 4) ЛУЧШАЯ СТАВКА НЕДЕЛИ
    if (
        "лучшая ставка недели" in text
        or ("лучш" in text and "ставк" in text and "недел" in text)
    ):
        return build_best_bet_insight(session, user_id)

    # 5) ОШИБКА НЕДЕЛИ
    if (
        "ошибка недели" in text
        or ("худш" in text and "ставк" in text and "недел" in text)
        or ("ошибк" in text and "недел" in text)
    ):
        return build_worst_bet_insight(session, user_id)

    # 6) РАЗБОР МОИХ РЫНКОВ
    if (
        "разбор моих рынков" in text
        or ("разбор" in text and "рынк" in text)
        or ("анализ" in text and "рынк" in text)
        or ("мои рынки" in text)
    ):
        return build_user_market_insights(session, user_id)

    # 7) ОБЩАЯ СТАТИСТИКА
    if (
        "статист" in text
        or "статку" in text
        or "stats" in text
        or "моя статистика" in text
    ):
        stats = get_user_stats(session, user_id)
        if stats.total_bets == 0:
            return "Пока нет ни одной сохранённой ставки. Начнём с первой 😉"

        if stats.settled_bets == 0 and stats.pushes == 0:
            return (
                f"Всего ставок: {stats.total_bets}\n"
                "Пока ни одна ставка не рассчитана.\n"
                "Когда отметишь результаты (например: 'ставка 1 выиграла' или 'ставка 2 возврат'), "
                "я посчитаю winrate и ROI."
            )

        text_lines = [
            "Твоя общая статистика:",
            f"Всего ставок: {stats.total_bets}",
            f"Рассчитано (win/lose): {stats.settled_bets}",
        ]
        if stats.pushes:
            text_lines.append(f"Возвратов: {stats.pushes}")
        if stats.settled_bets > 0:
            text_lines.extend(
                [
                    f"Винрейт: {stats.winrate:.1f}%",
                    f"ROI: {stats.roi:.2f}%",
                    f"Плюс/минус: {stats.pnl:.0f}",
                    f"Общий объём ставок: {stats.total_stake:.0f}",
                ]
            )

        bank = get_user_bank(session, user_id)
        text_lines.append("")
        if bank is not None:
            text_lines.append(_build_bank_status_text(bank))
            text_lines.append("")
        text_lines.append(
            "Отчёты и инсайты:\n"
            "• 'профиль'\n"
            "• 'отчёт за неделю'\n"
            "• 'лучшая ставка недели'\n"
            "• 'ошибка недели'\n"
            "• 'разбор моих рынков'\n"
            "• 'состояние банка'"
        )

        return "\n".join(text_lines)

    # 8) ОТЧЁТ ЗА НЕДЕЛЮ
    if (
        "отчёт за неделю" in text
        or "отчет за неделю" in text
        or ("отч" in text and "недел" in text)
    ):
        return build_weekly_report(session, user_id)

    # 9) АНАЛИЗ МАТЧА КХЛ ПО ID
    m_an = re.search(r"(анализ|разбор)\s+матча\s+(\d+)", text)
    if not m_an:
        m_an = re.search(r"(анализ|разбор)\s+(\d+)", text)
    if m_an:
        event_id_str = m_an.group(2)
        try:
            events = await get_today_khl_events()
        except Exception:
            logger.exception("Ошибка при получении матчей КХЛ (для анализа матча)")
            return (
                "Не смог получить матчи КХЛ для анализа (ошибка парсера или API).\n"
                "Попробуй ещё раз чуть позже или сначала запроси 'КХЛ сегодня'."
            )

        ev = None
        for e in events:
            if str(getattr(e, "id", "")) == event_id_str:
                ev = e
                break

        if ev is None:
            return (
                f"Я не нашёл матч с id {event_id_str} среди сегодняшних игр КХЛ.\n"
                "Сначала напиши 'КХЛ сегодня', выбери id матча из списка, а потом 'анализ матча <id>'."
            )

        return build_khl_match_analysis(ev)

    # 10) ОЦЕНКА СТАВКИ
    if (
        "оценка ставки" in text
        or ("что скажешь" in text and "ставк" in text)
        or ("как тебе" in text and "ставк" in text)
        or text.startswith("оценить ставку")
    ):
        return build_stake_evaluation(session, user_id, original_text)

    # 11) МАТЧИ КХЛ НА СЕГОДНЯ
    if "кхл" in text and ("сегодня" in text or "на сегодня" in text):
        try:
            events = await get_today_khl_events()
        except Exception:
            logger.exception("Ошибка при получении матчей КХЛ")
            return (
                "Не смог получить матчи КХЛ из источника "
                "(ошибка парсера или API бука).\n"
                "Попробуй ещё раз чуть позже или сформулируй другой запрос."
            )

        if not events:
            return "На сегодня я не нашёл матчей КХЛ."

        lines = []
        for e in events[:5]:
            line = f"{e.team1} — {e.team2} (id: {e.id})"

            market_1x2 = None
            for m in e.markets:
                name = getattr(m, "name", "") or ""
                if name.upper() in ("1X2", "1X", "3WAY", "3-WAY"):
                    market_1x2 = m
                    break

            if market_1x2:
                odds_part = ", ".join(
                    f"{o.name}: {o.price}" for o in market_1x2.outcomes
                )
                line += f" | 1X2: {odds_part}"

            lines.append(line)

        lines.append("")
        lines.append(
            "Чтобы получить разбор конкретного матча, напиши, например: 'анализ матча 123' "
            "(используй id из списка выше)."
        )

        return "Матчи КХЛ на сегодня:\n" + "\n".join(lines)

    # 12) МОИ СТАВКИ
    if "мои ставки" in text or ("ставки" in text and "мои" in text):
        bets = get_last_bets(session, user_id, limit=5)
        if not bets:
            return "У тебя пока нет сохранённых ставок."

        lines = []
               for b in bets:
            line_parts = [f"{b.created_at:%d.%m %H:%M} — {b.raw_text}"]
                   
            if b.event:
                line_parts.append(f"событие: {b.event}")
            if b.outcome:
                line_parts.append(f"исход: {b.outcome}")
            if b.stake:
                line_parts.append(f"сумма: {b.stake:g}")
            if b.odds:
                line_parts.append(f"кэф: {b.odds:.2f}")
            if b.result:
                if b.result == "win":
                    human_res = "выигрыш"
                elif b.result == "lose":
                    human_res = "проигрыш"
                elif b.result == "push":
                    human_res = "возврат"
                else:
                    human_res = b.result
                line_parts.append(f"результат: {human_res}")
            if b.profit is not None:
                sign = "+" if b.profit >= 0 else ""
                line_parts.append(f"PnL: {sign}{b.profit:.0f}")
            lines.append(" | ".join(line_parts))

        return "Твои последние ставки:\n" + "\n".join(lines)

    # 13) ДОБАВЛЕНИЕ СТАВКИ
    if text.startswith("ставка"):
        raw_text = original_text.strip()

        stake, odds = _parse_stake_and_odds(raw_text)
        outcome, event = _parse_outcome_and_event(raw_text)

        bet = add_bet(
            session=session,
            user_id=user_id,
            raw_text=raw_text,
            stake=stake,
            odds=odds,
            event=event,
            outcome=outcome,
        )

        resp_lines = [f"Ставка сохранена (id: {bet.id}).", "", f"Текст: {bet.raw_text}"]
        if event:
            resp_lines.append(f"Событие: {event}")
        if outcome:
            resp_lines.append(f"Исход: {outcome}")
        if stake is not None:
            resp_lines.append(f"Сумма: {stake:g}")
        if odds is not None:
            resp_lines.append(f"Коэффициент: {odds:.2f}")

        resp_lines.append(
            "\nКогда узнаешь результат, напиши, например:\n"
            f"'ставка {bet.id} выиграла', 'ставка {bet.id} проиграла' "
            f"или 'ставка {bet.id} возврат'.\n"
            "Посмотреть: 'мои ставки', 'профиль' или 'Покажи мою статистику'."
        )

        bank = get_user_bank(session, user_id)
        if bank is not None:
            resp_lines.extend(_build_bank_hint_for_stake(bank, stake))

        return "\n".join(resp_lines)

    # 14) ЗАГЛУШКИ
    if "аналити" in text and "матч" in text:
        return (
            "Раздел аналитики матчей расширяется.\n"
            "Уже сейчас можно:\n"
            "• запросить 'КХЛ сегодня' и увидеть матчи и линию 1X2\n"
            "• написать 'анализ матча <id>' для разбора линии по конкретному матчу."
        )

    if "live" in text or "лайв" in text or "жив" in text:
        return (
            "Live-инсайты пока в разработке.\n"
            "План: анализ темпа, xG по ходу матча и подсказки по тоталам."
        )

    if "премиум" in text or "premium" in text:
        return (
            "Премиум-режим пока не активирован.\n"
            "План: value-ставки, расширенная аналитика, персональные рекомендации."
        )

    # 15) HELP ПО УМОЛЧАНИЮ
    return (
        "Я AI-агент для ставок по хоккею.\n"
        "Сейчас умею:\n"
        "• Вести банк и подсказки по размеру ставки\n"
        "• Парсить сумму, кэф, исход и событие из текста ставки\n"
        "• Вести историю и показывать статистику\n"
        "• Делать weekly-отчёт и подсвечивать лучшую/худшую ставку недели\n"
        "• Разбирать твои рынки: 'разбор моих рынков'\n"
        "• Оценивать конкретную ставку как коуч: 'оценка ставки 1000 на СКА тотал больше 5.5 за 1.9'\n"
        "• Показывать матчи КХЛ и odds-разбор по матчу\n"
        "• Собираать твой профиль: 'профиль'\n\n"
        "Попробуй, например:\n"
        "• 'мой банк 100000'\n"
        "• 'профиль'\n"
        "• 'состояние банка'\n"
        "• 'оценка ставки 1000 на СКА тотал больше 5.5 за 1.9'\n"
        "• 'ставка 1000 на СКА - ЦСКА тотал больше 5.5 за 1.9'\n"
        "• 'мои ставки'\n"
        "• 'Покажи мою статистику'\n"
        "• 'отчёт за неделю'\n"
        "• 'лучшая ставка недели'\n"
        "• 'ошибка недели'\n"
        "• 'разбор моих рынков'\n"
        "• 'КХЛ сегодня'\n"
        "• 'анализ матча 123'\n"
        "• или напиши 'меню', чтобы увидеть основные разделы."
    )


# ------------------ ЗАПУСК TELEGRAM-БОТА В ФОНЕ ------------------


def _start_bot_background() -> None:
    """
    Стартуем Telegram-бота в отдельном потоке.
    Если TELEGRAM_BOT_TOKEN не задан — просто пишем варнинг и не запускаем бота.
    """
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            logger.warning(
                "TELEGRAM_BOT_TOKEN не задан; Telegram-бот не будет запущен."
            )
            return

        from . import telegram_bot

        logger.info("Запускаю Telegram-бота в фонового потоке...")
        t = threading.Thread(
            target=telegram_bot.main,
            name="telegram-bot-thread",
            daemon=True,
        )
        t.start()
    except Exception:
        logger.exception("Не удалось запустить Telegram-бота в фоне")


_start_bot_background()
