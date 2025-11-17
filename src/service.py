async def run_agent(user_id: int, message: str, session: Session) -> str:
    """
    Простейший if/else-агент.
    Дальше сюда можно будет наворачивать более умную логику и LLM.
    """
    original_text = message or ""
    text = original_text.lower().strip()

    # ----------------- 0) ГЛАВНОЕ МЕНЮ / СТАРТ -----------------
    if text in {"/start", "start", "меню", "главное меню", "help", "/help"}:
        return (
            "Я хоккейный AI-помощник для ставок 🏒\n\n"
            "Что я умею уже сейчас:\n"
            "🧾 *Мои ставки*\n"
            "  • сохранять ставки по тексту\n"
            "  • показывать последние\n"
            "  • считать winrate, ROI, PnL\n\n"
            "📊 *Аналитика матчей* (в разработке)\n"
            "  • разбор матчей КХЛ и других лиг\n"
            "  • подсказки по рынкам (тоталы, форы, 1X2)\n\n"
            "🔴 *Live-инсайты* (позже)\n"
            "  • анализ событий по ходу игры\n\n"
            "📈 *Отчёты недели* (позже)\n"
            "  • твой недельный отчёт по ставкам\n\n"
            "⭐ *Премиум* (позже)\n"
            "  • value-ставки, углублённая аналитика\n\n"
            "⚙️ *Настройки* (позже)\n\n"
            "Команды, которые уже работают:\n"
            "• 'ставка 1000 на СКА - ЦСКА тотал больше 5.5 за 1.9'\n"
            "• 'мои ставки'\n"
            "• 'покажи мою статистику'\n"
            "• 'ставка 1 выиграла' / 'ставка 2 возврат'\n"
            "• 'кхл сегодня'\n"
        )

    # ----------------- 1) ОТМЕТИТЬ РЕЗУЛЬТАТ СТАВКИ -----------------
    m_res = re.search(
        r"ставка\s+(\d+)\s+(выиграл[аи]?|проиграл[аи]?|выигрыш|проигрыш|возврат|refund|push|win|lose|loss)",
        text,
    )
    if m_res:
        bet_id = int(m_res.group(1))
        res_word = m_res.group(2)

        result = res_word  # settle_bet сам нормализует
        bet = settle_bet(session, user_id, bet_id, result)
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

        msg = f"Отметил ставку {bet.id} как {human_res}."
        if bet.profit is not None:
            sign = "+" if bet.profit >= 0 else ""
            msg += f"\nРезультат по сумме: {sign}{bet.profit:.0f}."
        msg += "\n\nПосмотреть обновлённую статистику: 'Покажи мою статистику'."
        return msg

    # ----------------- 2) СТАТИСТИКА ПОЛЬЗОВАТЕЛЯ -----------------
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
            "Твоя статистика:",
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

        return "\n".join(text_lines)

    # ----------------- 3) МАТЧИ КХЛ НА СЕГОДНЯ -----------------
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
        for e in events[:5]:  # ограничимся первыми 5 матчами
            line = f"{e.team1} — {e.team2} (id: {e.id})"

            # Пытаемся найти рынок 1X2 и показать коэффициенты
            market_1x2 = next((m for m in e.markets if m.name == "1X2"), None)
            if market_1x2:
                odds_part = ", ".join(
                    f"{o.name}: {o.price}" for o in market_1x2.outcomes
                )
                line += f" | 1X2: {odds_part}"

            lines.append(line)

        return "Матчи КХЛ на сегодня:\n" + "\n".join(lines)

    # ----------------- 4) МОИ СТАВКИ -----------------
    if "мои ставки" in text or ("ставки" in text and "мои" in text):
        from .bets_db import Bet  # чтобы взять result/profit при необходимости

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

    # ----------------- 5) ДОБАВЛЕНИЕ СТАВКИ -----------------
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
            "Посмотреть: 'мои ставки' или 'Покажи мою статистику'."
        )

        return "\n".join(resp_lines)

    # ----------------- 6) ЗАГЛУШКИ ПОД БУДУЩИЕ РАЗДЕЛЫ -----------------
    if "аналити" in text and "матч" in text:
        return (
            "Раздел аналитики матчей в разработке.\n"
            "Чуть позже тут будет разбор по xG, PP/PK, вратарям и value.\n"
            "Сейчас могу показать: 'КХЛ сегодня' или сохранить ставку."
        )

    if "live" in text or "лайв" in text or "жив" in text:
        return (
            "Live-инсайты пока в разработке.\n"
            "План: анализ темпа, xG по ходу матча и подсказки по тоталам."
        )

    if "отчёт" in text or "отчет" in text or "недел" in text:
        return (
            "Отчёты недели ещё не включены.\n"
            "Сначала накопим немного твоих ставок, потом я начну присылать сводки."
        )

    if "премиум" in text or "premium" in text:
        return (
            "Премиум-режим пока не активирован.\n"
            "План: value-ставки, расширенная аналитика, персональные рекомендации."
        )

    # ----------------- 7) HELP ПО УМОЛЧАНИЮ -----------------
    return (
        "Я AI-агент для ставок по хоккею.\n"
        "Сейчас умею:\n"
        "• Парсить сумму, кэф, исход (П1/П2/Х, тоталы, форы) и событие из текста ставки\n"
        "• По словам 'статистика / статку / моя статистика' показывать твою статистику\n"
        "• По запросу 'КХЛ сегодня' показывать матчи КХЛ\n"
        "• По сообщению вида 'ставка ...' сохранять ставку в базу\n"
        "• По запросу 'мои ставки' показывать последние сохранённые\n"
        "• По фразе 'ставка N выиграла/проиграла/возврат' отмечать результат и считать winrate/ROI\n\n"
        "Попробуй, например:\n"
        "• 'ставка 1000 на СКА - ЦСКА тотал больше 5.5 за 1.9'\n"
        "• 'мои ставки'\n"
        "• 'Покажи мою статистику'\n"
        "• 'Какие матчи КХЛ сегодня?'\n"
        "• или напиши 'меню', чтобы увидеть основные разделы."
    )
