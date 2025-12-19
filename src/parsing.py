from contextlib import contextmanager
from collections.abc import Generator, Iterator
from sqlmodel import Session

from .db import get_session


@contextmanager
def db_session() -> Iterator[Session]:
    """
    Берём Session через общий dependency get_session() (который yield-генератор).
    Закрываем генератор => срабатывает finally в get_session() => session.close().
    """
    gen = get_session()

    # get_session() должен быть генератором (yield Session)
    if not isinstance(gen, Generator):
        # на всякий случай (если где-то осталась старая версия get_session)
        session = gen  # type: ignore[assignment]
        try:
            yield session
        finally:
            try:
                session.close()
            except Exception:
                pass
        return

    session = next(gen)
    try:
        yield session
    finally:
        try:
            gen.close()
        except Exception:
            pass


# ------------------------------------------------------------
# УТИЛИТА ДЛЯ ПОЛУЧЕНИЯ SESSION ВНЕ FastAPI
# ------------------------------------------------------------

@contextmanager
def db_session() -> Session:
    """
    Аккуратно забираем Session из get_session(), который написан как fastapi-зависимость.
    """
    gen = get_session()
    session = next(gen)
    try:
        yield session
    finally:
        gen.close()


# ------------------------------------------------------------
# ВСПОМОГАТЕЛЬНОЕ ФОРМАТИРОВАНИЕ
# ------------------------------------------------------------

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
    """
    Поймать команды вроде:
    - "мой банк 100000"
    - "банк 50k"
    - "установи банк 200 000"
    """
    nums = re.findall(r"(\d+[ \d]*)", message.replace("\u00a0", " "))
    if not nums:
        return None
    num = nums[0].replace(" ", "")
    try:
        return float(num)
    except ValueError:
        return None


def _format_week_report(bets: List[bets_db.Bet]) -> str:
    """
    Очень простой недельный отчёт: считаем PnL, winrate по последним 7 дням.
    """
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
    lines.append("_Это базовый отчёт MVP. Позже я буду давать разбор по лигам и рынкам._")

    return "\n".join(lines)


# ------------------------------------------------------------
# ОСНОВНАЯ ЛОГИКА АГЕНТА (MVP без LLM)
# ------------------------------------------------------------

async def run_dialog_agent(user_id: int, message: str) -> str:
    """
    Главная функция «мозга» бота (MVP без LLM).
    """
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
    if "состояние банка" in norm or (("банк" in norm) and ("мой" in norm or "мне" in norm) and not re.search(r"\d", norm)):
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

    # 5) КХЛ сегодня
    if "кхл сегодня" in norm or "кхл на сегодня" in norm:
        try:
            text = await khl_today_text_from_winline()
            if not text:
                return "Пока не вижу линию КХЛ на сегодня. Попробуй чуть позже."
            return text
        except Exception as e:
            logger.exception("khl_today_text_from_winline failed: %s", e)
            return "Не удалось получить линию КХЛ на сегодня 😔 Попробуй чуть позже."

    # 6) Разбор моих рынков
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
                "В следующих версиях я буду делать полноценный разбор по рынкам, лигам и типам ставок."
            )

        lines = ["📊 *Разбор твоих рынков (MVP)*", ""]
        for outcome, pnl in sorted(by_outcome.items(), key=lambda x: -x[1]):
            lines.append(f"• {outcome}: *{pnl:+.0f}*")

        lines.append("")
        lines.append("_Это упрощённый разбор. В полной версии будет больше аналитики._")
        return "\n".join(lines)

    # 7) "ставка {id} выиграла/проиграла/возврат"
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

    # 8) Создание новой ставки (очень простой формат)
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
            "Когда узнаешь результат, нажми кнопку под ставкой или напиши:\n"
            f"`ставка {bet.id} выиграла` / `ставка {bet.id} проиграла` / `ставка {bet.id} возврат`."
        )

    # 9) Дефолтный ответ (пока без LLM)
    help_text = (
        "Пока я в MVP-версии и понимаю такие команды:\n\n"
        "• `профиль` — показать статистику и банк\n"
        "• `состояние банка` — показать текущий банк\n"
        "• `мой банк 100000` — установить банк\n"
        "• `КХЛ сегодня` — показать матчи и линию на сегодня\n"
        "• `отчёт за неделю` — краткий отчёт по ставкам\n"
        "• `разбор моих рынков` — базовый разбор рынков\n"
        "• `ставка: <событие>; исход=...; сумма=...; кэф=...` — сохранить ставку\n\n"
        "Позже я смогу делать полноценную аналитику матчей и рынков с помощью нейросети. "
        "Пока можешь протестировать эти команды 🙂"
    )

    return help_text
