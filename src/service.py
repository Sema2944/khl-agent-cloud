# src/service.py

from .hockey_logic import build_match_context_notes
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
from .khl_form_client import (
    get_team_form,
    TeamForm,
    TeamAdvancedForm,  # 👈 добавили
)
from .hockey_model import (
    build_team_strength_from_form,
    build_matchup_view,
)
import inspect

def get_team_advanced_form_safe(team_name: str) -> TeamAdvancedForm | None:
    """
    Заглушка для PRO-формы команды.

    Сейчас мы ещё не подключили реальный парсер продвинутой формы,
    поэтому аккуратно возвращаем None, чтобы не ломать бэкенд.
    Когда появится get_team_advanced_form в khl_form_client, просто
    обновим эту функцию и начнём использовать PRO-метрики.
    """
    return None



# 👇 запуск телеграм-бота
from .telegram_bot import main as run_telegram_bot

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
    - настраиваем логи
    - запускаем Telegram-бота в отдельном потоке
    """
    logging.basicConfig(level=logging.INFO)
    init_db()
    logger.info("FastAPI сервис запущен")

    def _run_tg_bot():
        try:
            logger.info("Запускаю Telegram-бота в фонового потоке...")
            run_telegram_bot()
        except Exception:
            logger.exception("Ошибка в Telegram-боте")

    t = threading.Thread(target=_run_tg_bot, name="telegram-bot", daemon=True)
    t.start()


@app.get("/")
def root():
    return {"status": "ok", "service": "khl-agent"}


# ---------- SAFE-ХЕЛПЕР ДЛЯ ФОРМЫ КОМАНДЫ ----------

async def get_team_form_safe(team_name: str) -> TeamForm | None:
    try:
        if inspect.iscoroutinefunction(get_team_form):
            return await get_team_form(team_name)
        return get_team_form(team_name)
    except Exception:
        logger.exception("Ошибка при получении формы команды %s", team_name)
        return None


# ---------- NEW: SAFE advanced form getter (PRO version) ----------
async def get_team_advanced_form_safe(team_name: str) -> TeamAdvancedForm | None:
    try:
        if inspect.iscoroutinefunction(get_team_advanced_form):
            return await get_team_advanced_form(team_name)
        return get_team_advanced_form(team_name)
    except Exception:
        logger.exception("Ошибка при получении TeamAdvancedForm для %s", team_name)
        return None



# ---------- API-ЭНДПОИНТЫ ДЛЯ АГЕНТА ----------

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


@app.get("/agent/last-bets")
async def agent_last_bets(
    user_id: int,
    limit: int = 5,
    session: Session = Depends(get_session),
):
    """
    Отдаём последние ставки пользователя в структурированном виде,
    чтобы Telegram-бот мог красиво показать их с кнопками.
    """
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


@app.post("/agent/settle-bet")
async def api_settle_bet(
    data: dict,
    session: Session = Depends(get_session),
):
    """
    Отмечаем ставку как win/lose/push по запросу Telegram-кнопки.
    """
    try:
        user_id = int(data.get("user_id"))
        bet_id = int(data.get("bet_id"))
        result = data.get("result")  # "win" | "lose" | "push"
    except Exception:
        return {"reply": "Некорректные данные для расчёта ставки."}

    # Рассчитываем ставку
    bet = settle_bet(session, user_id, bet_id, result)
    if bet is None:
        return {"reply": f"Ставка {bet_id} не найдена."}

    # Приводим результат в «человеческий» вид
    if result == "win":
        word = "выигрыш"
    elif result == "lose":
        word = "проигрыш"
    else:
        word = "возврат"

    lines: list[str] = [f"Ставка {bet_id} отмечена: {word}."]

    # PnL
    if bet.profit is not None:
        sign = "+" if bet.profit >= 0 else ""
        lines.append(f"PnL: {sign}{bet.profit:.0f}")

    # Обновляем банк
    bank = get_user_bank(session, user_id)
    if bank is not None and bet.profit is not None:
        user = change_user_bank(session, user_id, bet.profit)
        lines.append(f"Банк обновлён: {user.bank:.0f}")

    return {"reply": "\n".join(lines)}

# ↓ дальше оставляем твой существующий код: run_agent, build_khl_match_analysis и т.д.

async def api_settle_bet(
    data: dict,
    session: Session = Depends(get_session),
):
    """
    Отмечаем ставку как win/lose/push по запросу Telegram-кнопки.
    """
    try:
        user_id = int(data.get("user_id"))
        bet_id = int(data.get("bet_id"))
        result = data.get("result")  # "win" | "lose" | "push"
    except Exception:
        return {"reply": "Некорректные данные для расчёта ставки."}

    # Рассчитываем ставку
    bet = settle_bet(session, user_id, bet_id, result)
    if bet is None:
        return {"reply": f"Ставка {bet_id} не найдена."}

    # Приводим результат в «человеческий»
    if result == "win":
        word = "выигрыш"
    elif result == "lose":
        word = "проигрыш"
    else:
        word = "возврат"

    lines = [f"Ставка {bet_id} отмечена: {word}."]

    # PnL
    if bet.profit is not None:
            lines.append(f"PnL: {sign}{bet.profit:.0f}")

    # Обновляем банк
    bank = get_user_bank(session, user_id)
    if bank is not None and bet.profit is not None:
        user = change_user_bank(session, user_id, bet.profit)
        lines.append(f"Банк обновлён: {user.bank:.0f}")

    return {"reply": "\n".join(lines)}

    sign = "+" if bet.profit >= 0 else ""

# ------------------ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ПАРСИНГА ------------------


def _parse_stake_and_odds(raw_text: str) -> tuple[float | None, float | None]:
    """
    Выделяем сумму и коэффициент из произвольной строки.

    Особый кейс:
    - 'ставка на матч 123456 2000'
      → матч 123456 (ID), ставка 2000.
    """
    # Все числа с позициями
    num_matches = list(re.finditer(r"(\d+([\.,]\d+)?)", raw_text))
    if not num_matches:
        return None, None

    text_lower = raw_text.lower()

    # --- Определяем ID матча, если есть конструкция "матч 123456" ---
    match_id_index: int | None = None
    m_match = re.search(r"матч\s+(\d+)", text_lower)
    if m_match:
        match_id_str = m_match.group(1)
        # Ищем это число среди num_matches
        for idx, m in enumerate(num_matches):
            token = m.group(1)
            clean = token.replace(",", ".")
            # Для ID матча ожидаем целое число
            if clean.isdigit() and clean == match_id_str:
                match_id_index = idx
                break

    def _num_to_float(m: re.Match) -> float | None:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            return None

    stake: float | None = None
    odds: float | None = None
    stake_index: int | None = None

    # --- Логика выбора суммы ставки ---
    if match_id_index is not None:
        # 1) Сначала пытаемся найти число ПЕРЕД ID матча (ставка 2000 на матч 123456)
        if match_id_index > 0:
            stake_index = match_id_index - 1
        # 2) Иначе берём число ПОСЛЕ ID матча (ставка на матч 123456 2000)
        elif match_id_index + 1 < len(num_matches):
            stake_index = match_id_index + 1
    else:
        # Без "матч" → как раньше: первое число — сумма
        stake_index = 0

    if stake_index is not None:
        stake = _num_to_float(num_matches[stake_index])

    # --- Поиск коэффициента по ключевым словам "коэф/кф/за/по" ---

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
                    # защитимся от ситуации, когда кэф = сумме ставки
                    if not (
                        stake is not None
                        and stake >= 50
                        and abs(candidate_odds - stake) < 1e-9
                    ):
                        odds = candidate_odds
            except ValueError:
                pass

    # 3) Если всё ещё нет кэфа — берём первое подходящее число,
    #    которое не является ни ID матча, ни суммой
    if odds is None:
        for idx, m in enumerate(num_matches):
            if idx == stake_index or idx == match_id_index:
                continue
            candidate = _num_to_float(m)
            if candidate is None:
                continue
            if candidate >= 1.01:
                odds = candidate
                break

    return stake, odds



def _parse_outcome_and_event(raw_text: str) -> tuple[str | None, str | None]:
    """
    Пытаемся вытащить:
    - outcome: П1/П2/Х/1X/X2/12, тотал, фора и т.п.
    - event: текст о матче/командах (после 'на ...')

    Спец-кейс:
    - 'ставка на матч 123456 2000'
      → event = 'матч 123456'
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

    # ----- Спец-кейс: "на матч 123456" -----
    m_event_match = re.search(r"на\s+матч\s+(\d+)", text)
    if m_event_match:
        event = f"матч {m_event_match.group(1)}"
        return outcome, event

    # ----- ОБЩИЙ СЛУЧАЙ EVENT: текст после " на ..." -----
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
    # Важно: здесь латинская \d, а не русская "д"
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


# ------------------ ОТЧЁТ ЗА МЕСЯЦ ------------------


def build_monthly_report(session: Session, user_id: int) -> str:
    """
    Отчёт за последние 30 дней по ставкам пользователя.
    Берём все ставки пользователя и фильтруем по дате created_at.
    """
    bets_all = get_all_bets(session, user_id)

    now = datetime.utcnow()
    period_start = now - timedelta(days=30)

    # фильтруем только те, у кого есть created_at и он в пределах 30 дней
    bets = [
        b for b in bets_all
        if b.created_at is not None and b.created_at >= period_start
    ]

    if not bets:
        return (
            "За последние 30 дней у тебя не было записанных ставок. "
            "Как только набросаешь историю за месяц, я соберу подробный отчёт."
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
    lines.append(f"📈 Отчёт за последние 30 дней ({period_str}):")
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
        lines.append("🏆 Лучшая ставка месяца:")
        desc_best = best_bet.raw_text or ""
        sign_best = "+" if (best_bet.profit or 0) >= 0 else ""
        pnl_best = f"{sign_best}{(best_bet.profit or 0):.0f}"
        if best_bet.created_at:
            lines.append(f"• Дата: {best_bet.created_at:%d.%m %H:%M}")
        if desc_best:
            lines.append(f"• {desc_best}")
        lines.append(f"• Результат: {pnl_best}")

        lines.append("")
        lines.append("⚠️ Самая слабая ставка месяца:")
        desc_worst = worst_bet.raw_text or ""
        sign_worst = "+" if (worst_bet.profit or 0) >= 0 else ""
        pnl_worst = f"{sign_worst}{(worst_bet.profit or 0):.0f}"
        if worst_bet.created_at:
            lines.append(f"• Дата: {worst_bet.created_at:%d.%m %H:%M}")
        if desc_worst:
            lines.append(f"• {desc_worst}")
        lines.append(f"• Результат: {pnl_worst}")

    lines.append("")
    lines.append(
        "Месячный горизонт лучше показывает, где ты реально зарабатываешь, а где льёшь. "
        "Смотри на типы рынков, лиги и размеры ставок, которые тянут результат вниз."
    )

    return "\n".join(lines)



def build_monthly_report(session: Session, user_id: int) -> str:
    """
    Отчёт за последние 30 дней по ставкам пользователя.
    По структуре похож на недельный, но с другим периодом.
    """
    from .bets_db import Bet  # локальный импорт, чтобы не плодить циклы

    now = datetime.utcnow()
    period_start = now - timedelta(days=30)

    bets = session.exec(
        select(Bet).where(
            Bet.user_id == user_id,
            Bet.created_at >= period_start,
        )
    ).all()

    if not bets:
        return (
            "За последние 30 дней у тебя не было записанных ставок. "
            "Как только набросаешь историю за месяц, я соберу подробный отчёт."
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
    lines.append(f"📈 Отчёт за последние 30 дней ({period_str}):")
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
        lines.append("🏆 Лучшая ставка месяца:")
        desc_best = best_bet.raw_text or ""
        sign_best = "+" if (best_bet.profit or 0) >= 0 else ""
        pnl_best = f"{sign_best}{(best_bet.profit or 0):.0f}"
        if best_bet.created_at:
            lines.append(f"• Дата: {best_bet.created_at:%d.%m %H:%M}")
        if desc_best:
            lines.append(f"• {desc_best}")
        lines.append(f"• Результат: {pnl_best}")

        lines.append("")
        lines.append("⚠️ Самая слабая ставка месяца:")
        desc_worst = worst_bet.raw_text or ""
        sign_worst = "+" if (worst_bet.profit or 0) >= 0 else ""
        pnl_worst = f"{sign_worst}{(worst_bet.profit or 0):.0f}"
        if worst_bet.created_at:
            lines.append(f"• Дата: {worst_bet.created_at:%d.%m %H:%M}")
        if desc_worst:
            lines.append(f"• {desc_worst}")
        lines.append(f"• Результат: {pnl_worst}")

    lines.append("")
    lines.append(
        "Месячный горизонт лучше показывает, где ты реально зарабатываешь, а где льёшь. "
        "Смотри на типы рынков, лиги и размеры ставок, которые тянут результат вниз."
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
def build_value_analysis(raw_text: str) -> str:
    """
    Value-разбор кэфа:
    - парсим кэф (через существующий _parse_stake_and_odds)
    - парсим твою оценку вероятности (например '60%' или 'вероятность 60')
    - считаем:
        * имплайд-вероятность
        * 'справедливый' кэф по твоей оценке
        * edge (разница в п.п.)
        * ожидаемое матожидание (EV)
    """
    # 1) достаём кэф – используем уже готовый парсер
    _, odds = _parse_stake_and_odds(raw_text)

    # 2) достаём пользовательскую вероятность
    text = raw_text.lower()

    user_prob: float | None = None

    # вариант: '60%' / '60 %'
    m_pct = re.search(r"(\d+([\.,]\d+)?)\s*%", text)
    if m_pct:
        try:
            user_prob = float(m_pct.group(1).replace(",", "."))
        except ValueError:
            user_prob = None

    # вариант: 'вероятн 60', 'оценка 55', 'шанс 62'
    if user_prob is None:
        m_prob = re.search(
            r"(вероятн|оценк|шанс)[^\d]{0,10}(\d+([\.,]\d+)?)",
            text,
        )
        if m_prob:
            try:
                user_prob = float(m_prob.group(2).replace(",", "."))
            except ValueError:
                user_prob = None

    # ограничим адекватный диапазон
    if user_prob is not None:
        if not (0 < user_prob < 100):
            user_prob = None

    lines: list[str] = []
    lines.append("🎯 Value-разбор ставки:")

    if odds is None:
        lines.append("")
        lines.append(
            "Я не смог вытащить коэффициент из текста.\n"
            "Напиши что-то вроде: 'value ставка по 1.85 при вероятности 60%'."
        )
        return "\n".join(lines)

    # имплайд-вероятность по кэфу
    implied_prob = 100.0 / odds

    lines.append("")
    lines.append(f"Коэффициент: {odds:.2f}")
    lines.append(f"Имплайд-вероятность по рынку: ≈ {implied_prob:.1f}%")

    if user_prob is None:
        lines.append("")
        lines.append(
            "Ты не указал свою оценку вероятности.\n"
            "Чтобы я посчитал value, добавь, например: 'при вероятности 60%' или 'шанс 55%'."
        )
        lines.append("")
        lines.append(
            "Пример запроса:\n"
            "• 'value 1.85 при вероятности 60%'\n"
            "• 'value ставка по 2.10, шанс 48%'"
        )
        return "\n".join(lines)

    # 'справедливый' кэф по твоей оценке
    fair_odds_by_user = 100.0 / user_prob

    # edge в п.п. и EV
    edge_pp = user_prob - implied_prob  # +edge = value
    ev = odds * (user_prob / 100.0) - 1.0  # матожидание на 1 ед. ставки

    lines.append("")
    lines.append(f"Твоя оценка вероятности: ≈ {user_prob:.1f}%")
    lines.append(f"'Справедливый' кэф по твоей оценке: ≈ {fair_odds_by_user:.2f}")
    lines.append(f"Edge (разница): ≈ {edge_pp:.1f} п.п.")

    lines.append("")
    sign_ev = "+" if ev >= 0 else ""
    lines.append(f"Ожидаемое матожидание (EV) на 1 единицу ставки: {sign_ev}{ev:.3f}")
    if ev > 0:
        lines.append(
            "Это позитивное матожидание: при такой оценке вероятности ставка выглядит плюс-EV на дистанции."
        )
    elif ev < 0:
        lines.append(
            "Это отрицательное матожидание: при такой оценке вероятности ставка в минус-EV, "
            "рынок даёт хуже, чем твой 'справедливый' кэф."
        )
    else:
        lines.append(
            "Теоретически нулевое матожидание — линия примерно совпадает с твоей оценкой."
        )

    lines.append("")
    lines.append(
        "Важно: я не говорю 'ставь/не ставь'. Value-разбор — это чек-лист:\n"
        "• рынок → даёт имплайд-вероятность;\n"
        "• ты → даёшь свою оценку;\n"
        "• разница показывает, насколько линия лучше/хуже твоей модели."
    )

    return "\n".join(lines)
def _build_quick_bet_comment(
    stake: float | None,
    odds: float | None,
    outcome: str | None,
) -> list[str]:
    """
    Короткий человеческий комментарий к только что сохранённой ставке.
    Без сложной статистики, просто чек-лист по кэфу и типу рынка.
    """
    lines: list[str] = []
    lines.append("")
    lines.append("🧠 Быстрый комментарий по ставке:")

    # --- Разбор коэффициента ---
    if odds is not None:
        if odds < 1.30:
            lines.append(
                "• Очень низкий коэффициент. Такие ставки кажутся «надёжными», "
                "но маржа бука и редкие минусы могут съедать банк."
            )
        elif odds < 1.70:
            lines.append(
                "• Умеренный коэффициент — ставка ближе к фавориту, риск пониже, "
                "но и потенциал прибыли ограничен."
            )
        elif odds <= 2.50:
            lines.append(
                "• Рабочий диапазон коэффициентов — хороший баланс между риском и наградой."
            )
        else:
            lines.append(
                "• Высокий коэффициент — повышенный риск. Важно, чтобы у ставки была "
                "реальная аргументация, а не просто охота за большими кэфами."
            )

    # --- Разбор типа рынка ---
    if outcome:
        cat = _get_market_category(outcome)
        if cat == "тоталы":
            lines.append(
                "• Это ставка на тотал. Смотри на темп игры, качество атаки и спецбригады, "
                "а не только на ощущение «будет весёлый матч»."
            )
        elif cat == "форы":
            lines.append(
                "• Это ставка на фору. Важны частота побед/поражений с разницей в счёте "
                "и глубина состава, а не только статус фаворита."
            )
        elif cat == "1X2":
            lines.append(
                "• Это исход 1X2. Рынок тут обычно довольно точный, так что важно искать "
                "недооценённые стороны, а не просто любимую команду."
            )
        else:
            lines.append(
                "• Это менее стандартный рынок. Следи, чтобы таких ставок не становилось "
                "слишком много без понятной стратегии."
            )

    # Если вообще ничего не распознали
    if not (stake or odds or outcome):
        lines.append(
            "• Я сохранил ставку. Чем подробнее будешь указывать событие, исход и коэффициент, "
            "тем точнее смогу помогать дальше."
        )

    lines.append("")
    lines.append(
        "Это не команда «ставить / не ставить», а короткий чек-лист, чтобы ты сам оценил идею ставки."
    )
    return lines
def build_value_analysis(raw_text: str) -> str:
    """
    Простейший value-разбор коэффициента по тексту.
    Логика:
    1) Пытаемся вытащить кэф через общий парсер ставок (_parse_stake_and_odds)
       — он ловит конструкции типа 'по 1.9', 'кэф 2.1' и т.п.
    2) Если не получилось — просто ищем число в тексте и считаем, что это кэф.
    """
    # 1) Пытаемся вытащить кэф обычным парсером
    _, odds = _parse_stake_and_odds(raw_text)

    # 2) Если не нашли — пробуем просто взять число из текста
    if odds is None:
        num_matches = re.findall(r"(\d+([\.,]\d+)?)", raw_text)
        if num_matches:
            # Берём последнее число в строке — чаще всего это и есть кэф
            candidate_str = num_matches[-1][0]
            try:
                candidate = float(candidate_str.replace(",", "."))
                if candidate >= 1.01:
                    odds = candidate
            except ValueError:
                odds = None

    lines: list[str] = []
    lines.append("🎯 Проверка коэффициента (value-чеклист):")
    clean = raw_text.strip()
    if clean:
        lines.append(f"Текст: {clean}")
    lines.append("")

    if odds is None or odds < 1.01:
        lines.append(
            "Я не смог понять коэффициент из текста.\n"
            "Напиши, например:\n"
            "• 'value 1.85'\n"
            "• 'проверка кэф 2.3'\n"
            "• 'есть ли value в ставке по 1.70'"
        )
        return "\n".join(lines)

    # Имплайд-вероятность
    implied = 100.0 / odds
    lines.append(f"Коэффициент: {odds:.2f}")
    lines.append(f"Имплайд-вероятность (что закладывает букмекер): ≈ {implied:.1f}%")
    lines.append("")

    # Интерпретация диапазона кэфа
    if odds < 1.40:
        lines.append(
            "• Очень низкий кэф — рынок говорит, что событие почти обязано зайти.\n"
            "  В таких местах value редко бывает большим: маржа бука и редкие минусы сильно бьют по банку."
        )
    elif odds < 1.80:
        lines.append(
            "• Умеренный кэф — это зона фаворитов. Value чаще всего возникает, "
            "если рынок недооценил форму/состав или ты лучше читаешь матчап."
        )
    elif odds <= 3.00:
        lines.append(
            "• Рабочий диапазон кэфов. Здесь чаще всего и живут интересные value-ставки: "
            "всё решает твою оценку вероятности."
        )
    else:
        lines.append(
            "• Высокий кэф — это про редкие события. Value бывает, но важно не путать его "
            "с простым желанием поймать большой коэффициент."
        )

    lines.append("")
    lines.append("Как понять, есть ли value лично для тебя:")

    lines.append(
        "1) Спроси себя честно: *какова реальная вероятность, что ставка зайдёт?* "
        "Например, ты считаешь, что шанс 60%."
    )
    lines.append(
        "2) Посчитай свой 'честный' кэф: 100 / твоя_вероятность.\n"
        "   • Если думаешь, что шанс 60%, честный кэф ≈ 1.67.\n"
        "   • Если 45% — честный кэф ≈ 2.22."
    )
    lines.append(
        "3) Сравни с линией бука:\n"
        f"   • Если твой честный кэф НИЖЕ {odds:.2f} → для тебя это потенциальный value.\n"
        f"   • Если ВЫШЕ {odds:.2f} → рынок оценивает событие оптимистичнее, чем ты."
    )

    lines.append("")
    lines.append(
        "Важно: сам по себе коэффициент не 'хороший' и не 'плохой'. Value появляется только тогда, "
        "когда твоя оценка вероятности устойчиво лучше, чем у рынка на длинной дистанции."
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


def build_khl_match_analysis(ev) -> str:
    """
    Высокоуровневый разбор матча КХЛ.

    На выходе — готовый текст для бота:
    - линия 1X2 + имплайд-вероятности и "честные" проценты;
    - если получается — хоккейный матчап (темп, тоталы, вратари, риск апсета);
    - безопасно обрабатывает любые ошибки: в худшем случае показывает только линию.
    """

    team1_name = getattr(ev, "team1", "Команда 1")
    team2_name = getattr(ev, "team2", "Команда 2")
    event_id = getattr(ev, "id", "—")

    # --- 1. Находим рынок 1X2 ---
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

    # --- 2. Собираем коэффициенты 1 / X / 2 ---
    odds_map: dict[str, float] = {}
    for o in getattr(market_1x2, "outcomes", []) or []:
        key = (getattr(o, "name", "") or "").strip()
        price = getattr(o, "price", None)
        if not key or price is None:
            continue
        odds_map[key] = float(price)

    # Нормально обрабатываем разные обозначения (1, X, 2 / HOME, DRAW, AWAY и т.п.)
    def _pick_odds(*names: str) -> float | None:
        for n in names:
            if n in odds_map:
                return odds_map[n]
        return None

    odds_1 = _pick_odds("1", "HOME")
    odds_x = _pick_odds("X", "DRAW")
    odds_2 = _pick_odds("2", "AWAY")

    if odds_1 is None or odds_x is None or odds_2 is None:
        return (
            f"📊 Разбор матча КХЛ:\n"
            f"{team1_name} — {team2_name} (id: {event_id})\n\n"
            "По этому матчу нашёлся рынок, но не удалось корректно прочитать все три "
            "коэффициента 1X2. Показываю только то, что есть:\n"
            + "\n".join(f"• {k}: {v}" for k, v in odds_map.items())
        )

    # --- 3. Считаем имплайд-вероятности и "честные" проценты ---
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
    lines.append(
        f"• 1: кэф {odds_1:.2f}, импл. вероятность ≈ {imp_1:.1f}%"
    )
    lines.append(
        f"• X: кэф {odds_x:.2f}, импл. вероятность ≈ {imp_x:.1f}%"
    )
    lines.append(
        f"• 2: кэф {odds_2:.2f}, импл. вероятность ≈ {imp_2:.1f}%"
    )
    lines.append("")
    lines.append(f"Маржа букмекера по рынку 1X2 ≈ {margin:.1f} п.п.")
    lines.append("")
    lines.append("Оценка 'честных' вероятностей (без маржи бука):")
    lines.append(f"• 1: ≈ {fair_1:.1f}%")
    lines.append(f"• X: ≈ {fair_x:.1f}%")
    lines.append(f"• 2: ≈ {fair_2:.1f}%")

    # --- 4. Пытаемся построить хоккейный матчап (форма, темп, апсет и т.п.) ---
    try:
        form1: TeamForm | None = get_team_form(team1_name)
        form2: TeamForm | None = get_team_form(team2_name)
    except Exception:
        logger.exception("Ошибка при получении формы команд для анализа матча")
        form1 = form2 = None

    if not form1 or not form2:
        # Формы нет — честно говорим, что даём только линию
        lines.append("")
        lines.append(
            "Не удалось собрать форму команд — показываю только разбор линии 1X2."
        )
        lines.append("")
        lines.append(
            "Используй это как чек-лист по коэффициентам и вероятностям, "
            "а не как готовый прогноз. Для оценки value по конкретному исходу "
            "прогоняй кэф через команды вида 'value 1.85' или 'проверка кэф 2.3'."
        )
        return "\n".join(lines)

    # --- 5. Строим снимки силы команд и матчап через hockey_model ---
    try:
        snap1 = build_team_strength_from_form(team1_name, form1)
        snap2 = build_team_strength_from_form(team2_name, form2)
        matchup = build_matchup_view(snap1, snap2)
    except Exception:
        logger.exception("Ошибка при построении хоккейного матчапа")
        lines.append("")
        lines.append(
            "Форма команд частично недоступна, поэтому показываю только линию и базовые проценты."
        )
        return "\n".join(lines)

    # --- 6. Добавляем комментарии по темпу, тоталам, вратарям и риску апсета ---
    lines.append("")
    lines.append("📈 Хоккейный матчап (по модели):")

    # кто фаворит по модели
    if matchup.model_edge_side == "even":
        lines.append("• Модель видит матч близким к равному по суммарной силе.")
    elif matchup.model_edge_side == "team1":
        lines.append(
            f"• Небольшой перевес по модели на стороне {team1_name} "
            f"(уверенность ≈ {matchup.model_edge_confidence:.0f}%)."
        )
    else:
        lines.append(
            f"• Небольшой перевес по модели на стороне {team2_name} "
            f"(уверенность ≈ {matchup.model_edge_confidence:.0f}%)."
        )

    # темп / тотал
    lines.append(matchup.expected_pace_comment)
    lines.append(matchup.total_hint_comment)

    # физика / вратари / апсет
    lines.append(matchup.physicality_comment)
    lines.append(matchup.goalie_duel_comment)
    lines.append(f"Риск апсета: {matchup.upset_risk_level}. {matchup.upset_risk_comment}")

    # --- 7. Турнирный / мотивационный контекст через hockey_logic ---
    try:
        context_block = build_match_context_notes(
            team1_name=team1_name,
            team2_name=team2_name,
            matchup=matchup,
            form1=form1,
            form2=form2,
        )
    except TypeError:
        # если сигнатура другая / старая версия функции — не ломаемся
        try:
            context_block = build_match_context_notes(team1_name, team2_name)
        except Exception:
            logger.exception("Ошибка в build_match_context_notes (fallback)")
            context_block = ""
    except Exception:
        logger.exception("Ошибка в build_match_context_notes")
        context_block = ""

    if context_block:
        lines.append("")
        lines.append(context_block)

    # --- 8. Финальное напоминание про value и ответственность ---
    lines.append("")
    lines.append(
        "Используй этот разбор как чек-лист для своих решений по ставке, "
        "а не как готовый прогноз. Если выбираешь конкретный исход — "
        "прогоняй коэффициент через команды вида 'value 1.85' или "
        "'есть ли value в ставке по 2.10', чтобы оценить имплайд-вероятность "
        "и своё возможное преимущество над линией."
    )

    return "\n".join(lines)



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
            "  • 'анализ матча 123456' — демо-разбор линии по матчу СКА — ЦСКА\n\n"
            "📈 *Отчёты и инсайты по тебе*\n"
            "  • 'отчёт за неделю' — сводка по последним 7 дням\n"
            "  • 'отчёт за месяц' — сводка за 30 дней\n"
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
            "• 'отчёт за месяц'\n"
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

    # 8.1) ОТЧЁТ ЗА МЕСЯЦ
    if (
        "отчёт за месяц" in text
        or "отчет за месяц" in text
        or ("отч" in text and "месяц" in text)
    ):
        return build_monthly_report(session, user_id)

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

        # ВАЖНО: build_khl_match_analysis — async, поэтому его нужно await-ить
        try:
            return await build_khl_match_analysis(ev)
        except Exception:
            logger.exception("Ошибка внутри build_khl_match_analysis")
            return (
                "Не смог собрать детальный разбор матча (внутренняя ошибка агента).\n"
                "Я это поправлю, а пока можно пользоваться общими командами: "
                "'КХЛ сегодня', 'value 1.85', 'ставка 1000 на ...' и т.д."
            )





    # 10) ОЦЕНКА СТАВКИ
    if (
        "оценка ставки" in text
        or ("что скажешь" in text and "ставк" in text)
        or ("как тебе" in text and "ставк" in text)
        or text.startswith("оценить ставку")
    ):
        return build_stake_evaluation(session, user_id, original_text)

    # 10.1) VALUE-РАЗБОР КЭФА
    if (
        "value" in text
        or "вэлью" in text
        or "валю" in text
        or ("проверка" in text and "кэф" in text)
        or ("проверка" in text and "коэф" in text)
    ):
        return build_value_analysis(original_text)

    # 11) МАТЧИ КХЛ НА СЕГОДНЯ (демо-режим: один матч)
    if "кхл" in text and ("сегодня" in text or "на сегодня" in text):
        return (
            "Матчи КХЛ на сегодня:\n"
            "СКА — ЦСКА (id: 123456) | 1X2: 1: 1.85, X: 3.9, 2: 2.1\n\n"
            "Чтобы получить разбор конкретного матча, напиши, например: "
            "'анализ матча 123456' (используй id из списка выше)."
        )

    # 12) МОИ СТАВКИ
    if "мои ставки" in text or ("ставки" in text and "мои" in text):
        bets = get_last_bets(session, user_id, limit=5)
        if not bets:
            return "У тебя пока нет сохранённых ставок."

        lines: list[str] = []
        for b in bets:
            line_parts: list[str] = []

            if b.created_at:
                line_parts.append(f"{b.created_at:%d.%m %H:%M}")

            if b.raw_text:
                line_parts.append(b.raw_text)

            if b.event:
                line_parts.append(f"событие: {b.event}")
            if b.outcome:
                line_parts.append(f"исход: {b.outcome}")
            if b.stake is not None:
                line_parts.append(f"сумма: {b.stake:g}")
            if b.odds is not None:
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
            "\nКогда узнаешь результат, отметь его в боте:\n"
            f"• кнопкой под ставкой\n"
            f"• или текстом вида: 'ставка {bet.id} выиграла', 'ставка {bet.id} проиграла', 'ставка {bet.id} возврат'.\n"
            "Посмотреть историю: 'мои ставки', 'профиль' или 'Покажи мою статистику'."
        )

        bank = get_user_bank(session, user_id)
        if bank is not None:
            # Если банк уже задан — даём умный хинт про нагрузку на банк
            resp_lines.extend(_build_bank_hint_for_stake(bank, stake))
        else:
            # Если банк не задан — мягкий онбординг
            resp_lines.append(
                "\n💰 Банк пока не задан.\n"
                "Чтобы я мог считать нагрузку на банк и подсказывать размер ставки, "
                "задай его один раз, например: 'мой банк 100000'."
            )

        return "\n".join(resp_lines)


    # 14) ЗАГЛУШКИ
    if "аналити" in text and "матч" in text:
        return (
            "Раздел аналитики матчей расширяется.\n"
            "Уже сейчас можно:\n"
            "• запросить 'КХЛ сегодня' и увидеть демо-матч и линию 1X2\n"
            "• написать 'анализ матча 123456' для разбора линии по матчу СКА — ЦСКА."
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
        "• Делать weekly-/monthly-отчёты и подсвечивать лучшую/худшую ставку\n"
        "• Разбирать твои рынки: 'разбор моих рынков'\n"
        "• Оценивать конкретную ставку как коуч: 'оценка ставки 1000 на СКА тотал больше 5.5 за 1.9'\n"
        "• Показывать демо по КХЛ: 'КХЛ сегодня', 'анализ матча 123456'\n"
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
        "• 'отчёт за месяц'\n"
        "• 'лучшая ставка недели'\n"
        "• 'ошибка недели'\n"
        "• 'разбор моих рынков'\n"
        "• 'КХЛ сегодня'\n"
        "• 'анализ матча 123456'\n"
        "• или напиши 'меню', чтобы увидеть основные разделы."
    )

