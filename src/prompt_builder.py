# src/prompt_builder.py
"""
Prompt Builder: формирует промпты для LLM на основе MatchContext.
Разные промпты для PRE и LIVE.

Принцип: если данных нет — не выдумывай, напиши что данных нет.
"""
from __future__ import annotations

import logging
from typing import Optional

from .data_collector import MatchContext

logger = logging.getLogger(__name__)


def build_pre_prompt(ctx: MatchContext) -> str:
    """
    Промпт для PRE-анализа (до матча).
    Включает: линию, форму, H2H, составы, новости.
    """
    prompt = f"""Ты — профессиональный спортивный аналитик.
Проанализируй матч и дай конкретный вывод с рекомендациями.

МАТЧ: {ctx.home_team} — {ctx.away_team}
ЛИГА: {ctx.league} ({ctx.country})
ВРЕМЯ: {ctx.start_time}
"""

    # ── Кэфы ──
    if ctx.has_odds():
        o = ctx.odds
        # Определяем движение линии
        movement = "без движения"
        if o.home_win_open and o.home_win:
            if o.home_win < o.home_win_open:
                movement = f"→ в сторону {ctx.home_team} (было {o.home_win_open}, стало {o.home_win})"
            elif o.away_win_open and o.away_win < o.away_win_open:
                movement = f"→ в сторону {ctx.away_team} (было {o.away_win_open}, стало {o.away_win})"

        prompt += f"""
КЭФЫ ({o.bookmaker or 'рынок'}):
• {ctx.home_team}: {o.home_win} (откр: {o.home_win_open or '?'})
• {ctx.away_team}: {o.away_win} (откр: {o.away_win_open or '?'})
• Ничья: {o.draw or 'N/A'}
• Тотал {o.total_line}: больше {o.total_over} / меньше {o.total_under}
• Движение линии: {movement}
"""
    else:
        prompt += "\nКЭФЫ: нет данных\n"

    # ── Форма ──
    if ctx.has_form():
        hf = ctx.home_form
        af = ctx.away_form
        prompt += f"""
ФОРМА (сезон):
• {ctx.home_team}: {hf.last_10} | дома: {hf.home_record} | серия: {hf.streak or '?'} | голы: {hf.goals_per_game} заб / {hf.goals_against_per_game} проп
• {ctx.away_team}: {af.last_10} | гости: {af.away_record} | серия: {af.streak or '?'} | голы: {af.goals_per_game} заб / {af.goals_against_per_game} проп
"""
    else:
        prompt += "\nФОРМА: нет данных\n"

    # ── H2H ──
    if ctx.has_h2h():
        h = ctx.h2h
        prompt += f"""
H2H (последние встречи):
• Всего матчей: {h.total_games}
• {ctx.home_team}: {h.home_wins}W | {ctx.away_team}: {h.away_wins}W | Ничьи: {h.draws}
• Средний тотал: {h.avg_total}
• Последний матч: {h.last_result}
"""
    else:
        prompt += "\nH2H: нет данных\n"

    # ── Составы / травмы ──
    if ctx.home_info or ctx.away_info:
        prompt += "\nСОСТАВЫ:\n"
        if ctx.home_info:
            hi = ctx.home_info
            inj = ", ".join(hi.injuries) if hi.injuries else "нет данных"
            prompt += f"• {ctx.home_team}: травмы: {inj} | вратарь: {hi.goalie or '?'} | отдых: {hi.rest_days} дн.\n"
        if ctx.away_info:
            ai = ctx.away_info
            inj = ", ".join(ai.injuries) if ai.injuries else "нет данных"
            travel = f" | {ai.travel_info}" if ai.travel_info else ""
            prompt += f"• {ctx.away_team}: травмы: {inj} | вратарь: {ai.goalie or '?'}{travel}\n"

    # ── Новости ──
    if ctx.news:
        prompt += "\nНОВОСТИ (последние):\n"
        for n in ctx.news[:5]:
            prompt += f"• {n.get('title', '')}\n"

    # ── Полнота + формат ответа ──
    prompt += f"""
ПОЛНОТА ДАННЫХ: {ctx.data_completeness()}%

ФОРМАТ ОТВЕТА (строго):

📈 ЛИНИЯ
[движение кэфов и что это значит — конкретные цифры]

📋 ФОРМА
[ключевые моменты формы обеих команд]

⚔️ H2H
[история встреч, тренд]

🏥 СОСТАВЫ
[кто отсутствует, влияние на игру]

🎯 AI ВЫВОД
[главный вывод в 2-3 предложения]

→ [конкретная рекомендация 1] ([кэф]) — Confidence X/5
→ [конкретная рекомендация 2] ([кэф]) — Confidence X/5

⚠️ Риск: [главный риск]
⛔ Отмена: [при каком условии отменить]

ПРАВИЛА:
1. Если данных нет (?) — не выдумывай, напиши что данных нет
2. Confidence: 5/5 = все данные совпадают, 1/5 = почти нет данных
3. Конкретные кэфы. НЕ "равные шансы". НЕ "высокая конкуренция"
4. Каждое утверждение подкрепляй цифрой
5. Если данных меньше 30% — напиши "Недостаточно данных для анализа"
6. Это аналитический материал, не рекомендация к действию
"""

    return prompt


def build_live_prompt(ctx: MatchContext) -> str:
    """
    Промпт для LIVE-анализа (во время матча).
    Включает: live stats + кэфы + momentum.
    """
    score = f"{ctx.score_home}:{ctx.score_away}" if ctx.score_home is not None else "?:?"

    prompt = f"""Ты — профессиональный спортивный аналитик.
Дай LIVE-анализ матча с конкретными выводами.

МАТЧ: {ctx.home_team} — {ctx.away_team} | {ctx.league}
СЧЁТ: {score}

ВАЖНО — учитывай ТЕКУЩИЙ счёт:
- Если одна команда ведёт (например 1:0) — НЕ пиши про «равную борьбу» или «равные шансы»
- Если гол уже забит — НЕ пиши «первый гол сломает равновесие»
- При счёте 1:0 это «минимальное преимущество», а не «равная борьба»
- Описывай ситуацию в контексте текущего счёта
"""

    # ── LIVE статистика ──
    if ctx.has_live_stats():
        ls = ctx.live_stats
        prompt += f"""
ПЕРИОД: {ls.period} | ВРЕМЯ: {ls.time}

LIVE СТАТИСТИКА:
• Броски: {ctx.home_team} {ls.shots_home} — {ls.shots_away} {ctx.away_team}
• Вбрасывания: {ls.faceoffs_home}% — {ls.faceoffs_away}%
• Удаления (мин): {ls.penalties_home} — {ls.penalties_away}
• Большинство: {ctx.home_team} {ls.powerplay_home} | {ctx.away_team} {ls.powerplay_away}
• Хиты: {ls.hits_home} — {ls.hits_away}
• Блокшоты: {ls.blocked_home} — {ls.blocked_away}
"""
    else:
        prompt += "\nLIVE СТАТИСТИКА: недоступна (ожидание данных)\n"

    # ── LIVE кэфы ──
    if ctx.has_odds():
        o = ctx.odds
        prompt += f"""
LIVE КЭФЫ:
• {ctx.home_team}: {o.home_win} (откр: {o.home_win_open or '?'})
• {ctx.away_team}: {o.away_win} (откр: {o.away_win_open or '?'})
• Тотал {o.total_line}: б {o.total_over} / м {o.total_under}
"""

    prompt += f"""
ПОЛНОТА ДАННЫХ: {ctx.data_completeness()}%

ФОРМАТ ОТВЕТА (строго):

📊 КОНТРОЛЬ (MCI: X/100 → [кто доминирует])
[расчёт: броски 30% + владение 20% + линия 25% + momentum 25%]

📈 ЛИНИЯ LIVE
[как двигается линия и что это значит]

🔥 MOMENTUM
[кто давит последние 5-10 минут]

🎯 СЦЕНАРИЙ: [название]
[описание в 2-3 предложения]

→ [рекомендация 1] ([кэф]) — [почему сейчас]
→ [рекомендация 2] ([кэф]) — [почему сейчас]

⚠️ Триггер обновления: [при каком событии пересмотреть]
⛔ Отмена: [конкретное условие]

🛡 Confidence: X/5
[что совпадает и что нет]

ПРАВИЛА:
1. MCI считай из бросков + линии. НЕ из счёта!
2. Если бросков нет — не показывай MCI
3. Конкретные кэфы и тайминг
4. Если данных < 30% — напиши "Недостаточно данных для LIVE анализа"
5. Это аналитический материал, не рекомендация к действию
"""

    return prompt


def build_enriched_context_text(ctx: MatchContext) -> str:
    """
    Build a detailed text block with all available data for LLM enrichment.
    Used to append to existing prompts.
    Principle: every piece of data gets a concrete number or label.
    """
    parts = []

    # ── Кэфы и движение линии ──────────────────────────────────────────────
    if ctx.has_odds():
        o = ctx.odds
        home_line = f"{ctx.home_team} {o.home_win}"
        away_line = f"{ctx.away_team} {o.away_win}"

        # Определяем направление движения линии
        movement = ""
        if o.home_win_open and o.home_win and abs(o.home_win - o.home_win_open) >= 0.03:
            if o.home_win < o.home_win_open:
                pct = round((o.home_win_open - o.home_win) / o.home_win_open * 100, 1)
                movement = f" ↓ {o.home_win_open}→{o.home_win} (-{pct}%, рынок на хозяев)"
            else:
                pct = round((o.home_win - o.home_win_open) / o.home_win_open * 100, 1)
                movement = f" ↑ {o.home_win_open}→{o.home_win} (+{pct}%, рынок на гостей)"

        line_str = f"Линия: {home_line} | {away_line}"
        if o.draw:
            line_str += f" | X {o.draw}"
        if movement:
            line_str += f" | {movement}"
        if o.bookmaker:
            line_str += f" [{o.bookmaker}]"
        parts.append(line_str)

        if o.total_line:
            parts.append(
                f"Тотал {o.total_line}: больше {o.total_over} / меньше {o.total_under}"
                + (f" (откр {o.total_over_open})" if o.total_over_open else "")
            )

    # ── Форма команд ───────────────────────────────────────────────────────
    if ctx.has_form():
        hf = ctx.home_form
        af = ctx.away_form

        def _form_line(team: str, f) -> str:
            parts_f = [f"Форма {team}: {f.last_10}"]
            if f.home_record:
                parts_f.append(f"дома {f.home_record}" if team == ctx.home_team else f"гости {f.away_record}")
            if f.streak:
                parts_f.append(f"серия {f.streak}")
            goals_parts = []
            if f.goals_per_game:
                goals_parts.append(f"{f.goals_per_game:.1f} заб")
            if f.goals_against_per_game:
                goals_parts.append(f"{f.goals_against_per_game:.1f} проп")
            if goals_parts:
                parts_f.append(f"голы/игру: {' / '.join(goals_parts)}")
            return " | ".join(parts_f)

        parts.append(_form_line(ctx.home_team, hf))
        # Away: use away_record for guests
        af_line = f"Форма {ctx.away_team}: {af.last_10}"
        if af.away_record:
            af_line += f" | гости {af.away_record}"
        if af.streak:
            af_line += f" | серия {af.streak}"
        goals_parts = []
        if af.goals_per_game:
            goals_parts.append(f"{af.goals_per_game:.1f} заб")
        if af.goals_against_per_game:
            goals_parts.append(f"{af.goals_against_per_game:.1f} проп")
        if goals_parts:
            af_line += f" | голы/игру: {' / '.join(goals_parts)}"
        parts.append(af_line)

    # ── H2H ───────────────────────────────────────────────────────────────
    if ctx.has_h2h():
        h = ctx.h2h
        h2h_str = (
            f"H2H ({h.total_games} матчей): "
            f"{ctx.home_team} {h.home_wins}W — {h.draws}D — {h.away_wins}W {ctx.away_team}"
        )
        if h.avg_total:
            h2h_str += f" | ср.тотал {h.avg_total}"
        if h.last_result:
            h2h_str += f" | посл. матч: {h.last_result}"
        parts.append(h2h_str)

    # ── LIVE статистика ────────────────────────────────────────────────────
    if ctx.has_live_stats():
        ls = ctx.live_stats
        live_parts = []
        if ls.shots_home or ls.shots_away:
            live_parts.append(f"броски {ls.shots_home}-{ls.shots_away}")
        if ls.faceoffs_home:
            live_parts.append(f"вбрасывания {ls.faceoffs_home}%-{ls.faceoffs_away}%")
        if ls.hits_home or ls.hits_away:
            live_parts.append(f"хиты {ls.hits_home}-{ls.hits_away}")
        if ls.penalties_home or ls.penalties_away:
            live_parts.append(f"удаления {ls.penalties_home}м-{ls.penalties_away}м")
        if live_parts:
            parts.append("Статистика LIVE: " + " | ".join(live_parts))

    # ── Новости ────────────────────────────────────────────────────────────
    if ctx.news:
        titles = [n.get("title", "").strip() for n in ctx.news[:3] if n.get("title")]
        if titles:
            parts.append("Новости: " + " | ".join(titles))

    return "\n".join(parts)
