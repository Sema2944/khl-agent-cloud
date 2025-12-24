# src/parsing.py
from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple

from sqlmodel import Session

from .db import get_session
from . import bets_db
from .hockey_logic import khl_today_text_from_winline

logger = logging.getLogger(__name__)

# ============================================================
# DB SESSION (вне FastAPI)
# ============================================================

@contextmanager
def db_session() -> Session:
    """
    Берём Session через общий dependency get_session() (yield-генератор).
    """
    gen = get_session()
    session = next(gen)
    try:
        yield session
    finally:
        try:
            gen.close()
        except Exception:
            pass


# ============================================================
# НОРМАЛИЗАЦИЯ ЛИНИИ (ключевой блок)
# ============================================================

# Нормализованный рынок (универсально)
# Пример:
# {"type":"total","value":5.5,"over":1.87,"under":1.95}
# {"type":"moneyline","home":1.85,"draw":3.9,"away":2.1}
# {"type":"handicap","team":"home","value":-1.5,"odds":1.95}

def normalize_markets(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Приводим сырой ответ источника линии к единому формату.
    Сейчас raw — это то, что отдаст адаптер (пока демо).
    Позже сюда будет приходить OddsAPI/другой API.
    """
    out: List[Dict[str, Any]] = []
    markets = raw.get("markets") or []

    for m in markets:
        mtype = (m.get("type") or "").lower().strip()
        if mtype == "moneyline":
            home = m.get("home")
            away = m.get("away")
            draw = m.get("draw")
            # рынок должен быть полноценным (хотя бы home/away)
            if home is None or away is None:
                continue
            out.append({"type": "moneyline", "home": home, "draw": draw, "away": away})

        elif mtype == "total":
            value = m.get("value")
            over = m.get("over")
            under = m.get("under")
            if value is None or over is None or under is None:
                continue
            out.append({"type": "total", "value": float(value), "over": over, "under": under})

        elif mtype == "handicap":
            team = (m.get("team") or "").lower().strip()  # home/away
            value = m.get("value")
            odds = m.get("odds")
            if team not in ("home", "away") or value is None or odds is None:
                continue
            out.append({"type": "handicap", "team": team, "value": float(value), "odds": odds})

        # неизвестные рынки игнорируем

    return out


def format_markets_for_user(match: Dict[str, Any], markets: List[Dict[str, Any]]) -> str:
    """
    Красиво показываем рынки пользователю.
    """
    title = match.get("title") or "Матч"
    match_id = match.get("id") or "—"

    lines: List[str] = []
    lines.append(f"📈 *Линия на матч*")
    lines.append(f"{title}")
    lines.append(f"id: `{match_id}`")
    lines.append("")

    if not markets:
        lines.append("Пока нет доступных рынков (пусто или источник не отдал полные данные).")
        return "\n".join(lines)

    # Moneyline
    ml = [m for m in markets if m["type"] == "moneyline"]
    if ml:
        m = ml[0]
        lines.append("*1X2 / Moneyline*")
        lines.append(f"• П1: {m['home']}")
        if m.get("draw") is not None:
            lines.append(f"• X: {m['draw']}")
        lines.append(f"• П2: {m['away']}")
        lines.append("")

    # Totals
    totals = [m for m in markets if m["type"] == "total"]
    if totals:
        lines.append("*Тоталы (Over/Under)*")
        for t in totals[:6]:
            lines.append(f"• {t['value']}: O {t['over']} / U {t['under']}")
        lines.append("")

    # Handicap
    hcaps = [m for m in markets if m["type"] == "handicap"]
    if hcaps:
        lines.append("*Форы (Handicap)*")
        for h in hcaps[:8]:
            team = "Хозяева" if h["team"] == "home" else "Гости"
            lines.append(f"• {team} {h['value']:+.1f}: {h['odds']}")
        lines.append("")

    lines.append("Чтобы получить объяснение линии: `аналитика <id>`")
    return "\n".join(lines)


def explain_line(match: Dict[str, Any], markets: List[Dict[str, Any]]) -> str:
    """
    MVP-аналитика без LLM:
    - объясняем, что значит линия
    - без 'ставь/не ставь'
    - без токсичности
    """
    title = match.get("title") or "Матч"
    lines: List[str] = []
    lines.append("🧠 *Аналитика линии (MVP)*")
    lines.append(f"{title}")
    lines.append("")
    if not markets:
        lines.append("По этому матчу нет достаточных данных линии, чтобы сделать разбор.")
        return "\n".join(lines)

    # Moneyline interpretation
    ml = next((m for m in markets if m["type"] == "moneyline"), None)
    if ml:
        home = float(ml["home"])
        away = float(ml["away"])
        draw = ml.get("draw")

        # грубая “имплайд” вероятность (без маржи) — только для объяснения
        p_home = 1.0 / home if home > 1.01 else 0.0
        p_away = 1.0 / away if away > 1.01 else 0.0
        p_draw = 1.0 / float(draw) if (draw is not None and float(draw) > 1.01) else 0.0
        s = p_home + p_away + p_draw
        if s > 0:
            p_home = p_home / s
            p_away = p_away / s
            p_draw = p_draw / s if p_draw > 0 else 0.0

        lines.append("*Что говорит 1X2:*")
        lines.append(
            f"• По коэффициентам рынок считает хозяев чуть сильнее/слабее в зависимости от цифр."
        )
        lines.append(
            f"• Примерная доля вероятностей (после нормировки): П1 ~ {p_home*100:.0f}%, П2 ~ {p_away*100:.0f}%"
            + (f", X ~ {p_draw*100:.0f}%" if p_draw > 0 else "")
        )
        lines.append("")

    # Totals interpretation
    totals = [m for m in markets if m["type"] == "total"]
    if totals:
        main_total = sorted(totals, key=lambda x: abs(float(x["value"]) - 5.5))[0]  # условный “центр”
        lines.append("*Что говорит тотал:*")
        lines.append(
            f"• Линия тотала {main_total['value']} — это ожидание результативности матча."
        )
        lines.append(
            f"• Баланс O/U близкий → рынок не уверен, где будет итог по шайбам."
        )
        lines.append("")

    lines.append("⚠️ Это информационный разбор линии, а не рекомендация ставить.")
    return "\n".join(lines)


# ============================================================
# ИСТОЧНИК ЛИНИИ (адаптер, чтобы потом заменить API)
# ============================================================

async def get_today_khl_matches() -> List[Dict[str, Any]]:
    """
    MVP: пока отдаём демо-матчи (или текст из winline парсера).
    Здесь позже будет нормальный источник матчей (OddsAPI/др).
    """
    # Если твой winline-парсер уже возвращает текст — оставляем это для команды "КХЛ сегодня"
    # А матчи для "линия <id>" пока демо.
    return [
        {"id": "demo_khl_123456", "title": "СКА — ЦСКА", "league": "KHL"},
    ]


async def get_raw_line_for_match(match_id: str) -> Optional[Dict[str, Any]]:
    """
    Адаптер линии:
    - сегодня: демо
    - завтра: здесь дергаем OddsAPI/другой API и возвращаем raw
    """
    # Демо-линия для demo_khl_123456
    if match_id == "demo_khl_123456":
        return {
            "match": {"id": match_id, "title": "СКА — ЦСКА", "league": "KHL"},
            "markets": [
                {"type": "moneyline", "home": 1.85, "draw": 3.90, "away": 2.10},
                {"type": "total", "value": 5.5, "over": 1.87, "under": 1.95},
                {"type": "handicap", "team": "home", "value": -1.5, "odds": 2.25},
                {"type": "handicap", "team": "away", "value": +1.5, "odds": 1.60},
            ],
        }
    return None


# ============================================================
# ВСПОМОГАТЕЛЬНОЕ ФОРМАТИРОВАНИЕ
# ============================================================

def _format_profile_text(bank: Optional[float], stats: bets_db.UserStats) -> str:
    lines: list[str] = []
    lines.append("📊 *Твой профиль*")

    if bank is None:
        lines.append("Банк: _ещё не задан_")
        lines.append("Совет: задай банк командой вроде: `мой банк 100000`")
    else:
        lines.append(f"Банк: *{bank:,.0f}*".replace(",", " "))

    lines.append("")
    lines.append(f"Всего ставок: *{stats.total_bets}*")
    lines.append(f"Рассчитано ставок (без возвратов): *{stats.settled_bets}*")
    lines.append(f"Возвратов: *{stats.pushes}*")
    lines.append(f"Winrate: *{stats.winrate:.1f}%*")
    lines.append(f"ROI: *{stats.roi:.1f}%*")
    lines.append(f"PnL: *{stats.pnl:+.0f}*")
    lines.append(f"Объём ставок: *{stats.total_stake:.0f}*")
    lines.append("")
    lines.append("Это упрощённая статистика по всем твоим ставкам.")
    return "\n".join(lines)


def _parse_bank_set(message: str) -> Optional[float]:
    nums = re.findall(r"(\d+[ \d]*)", message.replace("\u00a0", " "))
    if not nums:
        return None
    num = nums[0].replace(" ", "")
    try:
        return float(num)
    except ValueError:
        return None


def _format_week_report(bets: List[bets_db.Bet]) -> str:
    if not bets:
        return (
            "За последнюю неделю у тебя не было сохранённых ставок.\n"
            "Начни добавлять ставки, и я смогу делать отчёты по рынкам и результатам."
        )

    wins = [b for b in bets if b.result == "win"]
    loses = [b for b in bets if b.result == "lose"]
    pushes = [b for b in bets if b.result == "push"]
    non_push = wins + loses

    settled = len(non_push)
    pnl = sum(b.profit or 0.0 for b in non_push)
    total_stake = sum(b.stake or 0.0 for b in non_push)
    winrate = (len(wins) / settled * 100.0) if settled > 0 else 0.0
    roi = (pnl / total_stake * 100.0) if total_stake > 0 else 0.0

    lines: list[str] = []
    lines.append("📆 *Отчёт за последние 7 дней*")
    lines.append(f"Всего ставок: *{len(bets)}*")
    lines.append(f"Рассчитано (без возвратов): *{settled}*")
    lines.append(f"Возвратов: *{len(pushes)}*")
    lines.append(f"Winrate: *{winrate:.1f}%*")
    lines.append(f"ROI: *{roi:.1f}%*")
    lines.append(f"PnL: *{pnl:+.0f}*")
    lines.append(f"Объём ставок: *{total_stake:.0f}*")
    lines.append("")
    lines.append("_Это базовый отчёт MVP. Позже будет разбор по лигам и рынкам._")
    return "\n".join(lines)


# ============================================================
# ОСНОВНАЯ ЛОГИКА АГЕНТА
# ============================================================

async def run_dialog_agent(user_id: int, message: str) -> str:
    text_raw = message or ""
    norm = text_raw.lower().strip()

    logger.info("run_dialog_agent: user_id=%s, norm=%r", user_id, norm)

    # 1) Профиль
    if "профиль" in norm:
        with db_session() as session:
            bank = bets_db.get_user_bank(session, user_id)
            stats = bets_db.get_user_stats(session, user_id)
        return _format_profile_text(bank, stats)

    # 2) Состояние банка
    if "состояние банка" in norm or (
        ("банк" in norm) and ("мой" in norm or "мне" in norm) and not re.search(r"\d", norm)
    ):
        with db_session() as session:
            bank = bets_db.get_user_bank(session, user_id)
        if bank is None:
            return (
                "У тебя пока не задан банк.\n\n"
                "Можешь установить его командой вроде:\n"
                "`мой банк 100000`"
            )
        return f"Текущий банк: *{bank:,.0f}*".replace(",", " ")

    # 3) Установка банка
    if "банк" in norm:
        new_bank = _parse_bank_set(norm)
        if new_bank is not None:
            with db_session() as session:
                user = bets_db.set_user_bank(session, user_id, new_bank)
            return f"Банк установлен: *{user.bank:,.0f}*".replace(",", " ")

    # 4) Отчёт за неделю
    if "отчёт за неделю" in norm or "отчет за неделю" in norm:
        now = datetime.utcnow()
        week_ago = now - timedelta(days=7)
        with db_session() as session:
            all_bets = bets_db.get_all_bets(session, user_id)
        last_week_bets = [b for b in all_bets if b.created_at >= week_ago]
        return _format_week_report(last_week_bets)

    # 5) КХЛ сегодня (как было)
    if "кхл сегодня" in norm or "кхл на сегодня" in norm:
        try:
            text = await khl_today_text_from_winline()
            if not text:
                # fallback на демо матч-лист
                matches = await get_today_khl_matches()
                if not matches:
                    return "Пока не вижу линию КХЛ на сегодня. Попробуй чуть позже."
                lines = ["🏒 Матчи КХЛ на сегодня:", ""]
                for i, m in enumerate(matches, 1):
                    lines.append(f"{i}) {m['title']} (id: {m['id']})")
                lines.append("")
                lines.append("Чтобы получить линию, напиши: `линия <id>`")
                return "\n".join(lines)
            return text
        except Exception as e:
            logger.exception("khl_today_text_from_winline failed: %s", e)
            # fallback
            matches = await get_today_khl_matches()
            lines = ["🏒 Матчи КХЛ на сегодня:", ""]
            for i, m in enumerate(matches, 1):
                lines.append(f"{i}) {m['title']} (id: {m['id']})")
            lines.append("")
            lines.append("Чтобы получить линию, напиши: `линия <id>`")
            return "\n".join(lines)

    # 6) Линия матча: "линия <id>"
    m_line = re.match(r"линия\s+([a-z0-9_\-]+)", norm)
    if m_line:
        match_id = m_line.group(1).strip()
        raw = await get_raw_line_for_match(match_id)
        if not raw:
            return "Не нашёл матч по этому id. Сначала запроси: `КХЛ сегодня` и возьми id."
        match = raw.get("match") or {"id": match_id, "title": match_id}
        markets = normalize_markets(raw)
        return format_markets_for_user(match, markets)

    # 7) Аналитика линии: "аналитика <id>"
    m_an = re.match(r"аналитика\s+([a-z0-9_\-]+)", norm)
    if m_an:
        match_id = m_an.group(1).strip()
        raw = await get_raw_line_for_match(match_id)
        if not raw:
            return "Не нашёл матч по этому id. Сначала запроси: `КХЛ сегодня` и возьми id."
        match = raw.get("match") or {"id": match_id, "title": match_id}
        markets = normalize_markets(raw)
        return explain_line(match, markets)

    # 8) Разбор моих рынков
    if "разбор моих рынков" in norm:
        with db_session() as session:
            bets = bets_db.get_all_bets(session, user_id)

        if not bets:
            return (
                "У тебя пока нет сохранённых ставок, чтобы разобрать рынки.\n"
                "Начни фиксировать ставки — и я смогу показать, где ты зарабатываешь, а где сливаешь."
            )

        by_outcome: dict[str, float] = {}
        for b in bets:
            if not b.outcome:
                continue
            by_outcome.setdefault(b.outcome, 0.0)
            by_outcome[b.outcome] += float(b.profit or 0.0)

        if not by_outcome:
            return (
                "Ставки есть, но по ним пока мало структурированных данных.\n"
                "Добавляй ставки форматом: `ставка: матч; исход=...; сумма=...; кэф=...`"
            )

        lines = ["📊 *Разбор твоих рынков (MVP)*", ""]
        for outcome, pnl in sorted(by_outcome.items(), key=lambda x: -x[1]):
            lines.append(f"• {outcome}: *{pnl:+.0f}*")

        lines.append("")
        lines.append("_Это упрощённый разбор. В полной версии будет больше аналитики._")
        return "\n".join(lines)

    # 9) "ставка {id} выиграла/проиграла/возврат"
    m_res = re.match(r"ставка\s+(\d+)\s+(.+)", norm)
    if m_res:
        bet_id = int(m_res.group(1))
        result_text = m_res.group(2).strip()

        with db_session() as session:
            bet = bets_db.settle_bet(session, user_id, bet_id, result_text)

        if bet is None:
            return "Не удалось найти ставку или понять результат 😔"

        human = {"win": "выигрыш", "lose": "проигрыш", "push": "возврат"}.get(
            bet.result or "", bet.result
        )
        pnl = bet.profit if bet.profit is not None else 0.0
        sign = "+" if pnl >= 0 else ""
        return f"Ставка #{bet.id} отмечена как *{human}*, PnL: *{sign}{pnl:.0f}*."

    # 10) Создание новой ставки (простой формат)
    if norm.startswith("ставка"):
        body = text_raw.split("ставка", 1)[1]
        body = body.lstrip(" :")

        parts = [p.strip() for p in body.split(";") if p.strip()]
        event = None
        outcome = None
        stake = None
        odds = None

        if parts:
            first = parts[0].lower()
            if not any(key in first for key in ("исход", "сумма", "кэф", "коэф", "коэф.")):
                event = parts[0]

        for p in parts:
            pl = p.lower()
            if pl.startswith("исход"):
                outcome = p.split("=", 1)[-1].strip()
            elif pl.startswith("сумма") or pl.startswith("stake"):
                val = p.split("=", 1)[-1]
                val = re.sub(r"[^\d.,]", "", val).replace(",", ".")
                try:
                    stake = float(val)
                except ValueError:
                    pass
            elif pl.startswith("кэф") or pl.startswith("коэф") or pl.startswith("коэффициент"):
                val = p.split("=", 1)[-1]
                val = re.sub(r"[^\d.,]", "", val).replace(",", ".")
                try:
                    odds = float(val)
                except ValueError:
                    pass

        with db_session() as session:
            bet = bets_db.add_bet(
                session=session,
                user_id=user_id,
                raw_text=text_raw,
                stake=stake,
                odds=odds,
                event=event,
                outcome=outcome,
            )

        return (
            f"Ставка сохранена (id: {bet.id}).\n\n"
            "Когда узнаешь результат, напиши:\n"
            f"`ставка {bet.id} выиграла` / `ставка {bet.id} проиграла` / `ставка {bet.id} возврат`."
        )

    # 11) Help
    return (
        "Пока я в MVP-версии и понимаю такие команды:\n\n"
        "• `профиль` — показать статистику и банк\n"
        "• `состояние банка` — показать текущий банк\n"
        "• `мой банк 100000` — установить банк\n"
        "• `КХЛ сегодня` — показать матчи на сегодня\n"
        "• `линия <id>` — показать линию по матчу\n"
        "• `аналитика <id>` — объяснить линию (без советов)\n"
        "• `отчёт за неделю` — краткий отчёт по ставкам\n"
        "• `разбор моих рынков` — базовый разбор рынков\n"
        "• `ставка: <событие>; исход=...; сумма=...; кэф=...` — сохранить ставку\n\n"
        "Дальше подключим реальный источник линии (OddsAPI или другой) через адаптер."
    )
