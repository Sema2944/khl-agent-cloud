# src/scheduled_posts.py
"""
Scheduled channel posts: background loop + CRUD + bulk loader.

Table: scheduled_posts (created in db.py bootstrap migrations).
Background loop checks every 60 seconds for posts that are due.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import text

from .db import engine

logger = logging.getLogger(__name__)

CHANNEL_USERNAME = (os.getenv("CHANNEL_USERNAME") or "").strip()
MSK = ZoneInfo("Europe/Moscow")


# ------------------------------------------------------------------
# CRUD helpers (sync, use engine directly — same pattern as db.py)
# ------------------------------------------------------------------

def add_scheduled_post(
    post_text: str,
    publish_at: datetime,
    pinned: bool = False,
) -> int:
    """Insert a scheduled post. Returns the new row id."""
    with engine.begin() as conn:
        row = conn.execute(
            text("""
                INSERT INTO scheduled_posts (text, publish_at, pinned)
                VALUES (:t, :p, :pin)
                RETURNING id
            """),
            {"t": post_text, "p": publish_at, "pin": pinned},
        ).fetchone()
        return row[0] if row else 0


def list_scheduled_posts(only_pending: bool = True) -> List[dict]:
    """Return scheduled posts (pending by default)."""
    with engine.connect() as conn:
        if only_pending:
            rows = conn.execute(
                text("SELECT id, text, publish_at, pinned FROM scheduled_posts WHERE published = FALSE ORDER BY publish_at")
            ).fetchall()
        else:
            rows = conn.execute(
                text("SELECT id, text, publish_at, published, pinned FROM scheduled_posts ORDER BY publish_at")
            ).fetchall()
        result = []
        for r in rows:
            d = {"id": r[0], "text": r[1], "publish_at": r[2], "pinned": r[3]}
            if not only_pending:
                d["published"] = r[3]
                d["pinned"] = r[4]
            result.append(d)
        return result


def delete_scheduled_post(post_id: int) -> bool:
    """Delete a scheduled post by id. Returns True if deleted."""
    with engine.begin() as conn:
        res = conn.execute(
            text("DELETE FROM scheduled_posts WHERE id = :id AND published = FALSE"),
            {"id": post_id},
        )
        return res.rowcount > 0


def get_due_posts() -> List[dict]:
    """Return posts where publish_at <= now AND published = FALSE."""
    now_utc = datetime.utcnow()
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT id, text, pinned
                FROM scheduled_posts
                WHERE published = FALSE AND publish_at <= :now
                ORDER BY publish_at
            """),
            {"now": now_utc},
        ).fetchall()
        return [{"id": r[0], "text": r[1], "pinned": r[2]} for r in rows]


def mark_published(post_id: int, message_id: int) -> None:
    """Mark post as published with the Telegram message_id."""
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE scheduled_posts
                SET published = TRUE, published_at = NOW(), message_id = :mid
                WHERE id = :id
            """),
            {"id": post_id, "mid": message_id},
        )


# ------------------------------------------------------------------
# Background loop
# ------------------------------------------------------------------

async def scheduled_posts_loop(bot) -> None:
    """Check every 60s for posts that are due, send to channel."""
    await asyncio.sleep(15)  # let app warm up
    logger.info("Scheduled posts loop started (every 60s)")

    while True:
        try:
            if not CHANNEL_USERNAME:
                await asyncio.sleep(60)
                continue

            due = get_due_posts()
            for post in due:
                try:
                    msg = await bot.send_message(
                        chat_id=CHANNEL_USERNAME,
                        text=post["text"],
                    )
                    mark_published(post["id"], msg.message_id)
                    logger.info("Scheduled post #%d published to %s (msg_id=%d)",
                                post["id"], CHANNEL_USERNAME, msg.message_id)

                    # Pin if needed
                    if post["pinned"]:
                        try:
                            await bot.pin_chat_message(
                                chat_id=CHANNEL_USERNAME,
                                message_id=msg.message_id,
                                disable_notification=True,
                            )
                            logger.info("Scheduled post #%d pinned", post["id"])
                        except Exception:
                            logger.exception("Failed to pin post #%d", post["id"])

                except Exception:
                    logger.exception("Failed to publish scheduled post #%d", post["id"])

        except asyncio.CancelledError:
            logger.info("Scheduled posts loop cancelled")
            break
        except Exception:
            logger.exception("Scheduled posts loop error")

        await asyncio.sleep(60)


# ------------------------------------------------------------------
# /loadposts — bulk load 20 predefined posts for first week
# ------------------------------------------------------------------
# Post texts and schedule are loaded lazily (only on /loadposts call)
# to avoid keeping ~15KB of strings in memory permanently.
# ------------------------------------------------------------------


def _get_pinned_post() -> str:
    """Return pinned post text (created on demand, not kept in module scope)."""
    return (
        "🎯 Betly — AI-аналитика спорта\n\n"
        "Каждый день в 11:00 МСК — Топ-3 матча дня.\n"
        "AI анализирует 500+ матчей и выбирает лучшие.\n\n"
        "7 видов спорта: футбол, хоккей, баскетбол, теннис, MMA, волейбол, Формула 1.\n\n"
        "КЭФы от Pinnacle, Bet365, 1xBet.\n"
        "Движение линии и H2H-статистика.\n\n"
        "Полные прогнозы и AI-разбор — в боте:\n"
        "👉 @BetlyAIBot\n\n"
        "🎁 Промокод BETLY2026 — 7 дней PRO бесплатно"
    )


def _get_schedule_and_posts() -> Tuple[list, list]:
    """Return (schedule, post_texts) — created on demand, GC'd after use."""
    # (day, hour, minute) in MSK
    schedule: list[Tuple[int, int, int]] = [
        (1, 9, 0), (1, 15, 0), (1, 21, 0),   # Day 1
        (2, 9, 0), (2, 15, 0), (2, 21, 0),   # Day 2
        (3, 9, 0), (3, 15, 0), (3, 21, 0),   # Day 3
        (4, 9, 0), (4, 16, 0),                # Day 4
        (5, 9, 0), (5, 15, 0), (5, 21, 0),   # Day 5
        (6, 9, 0), (6, 16, 0),                # Day 6
        (7, 9, 0), (7, 15, 0), (7, 21, 0),   # Day 7
    ]

    # 19 post texts (Day 1 → Day 7), order matches schedule
    post_texts: list[str] = [
    # Day 1 — Post 1 (09:00)
    """🔬 Как работает Betly?

Каждое утро наш AI-движок обрабатывает данные по 500+ матчам:

— Форма команд за последние 5-10 игр
— Статистика личных встреч (H2H)
— КЭФы от 5 букмекеров в реальном времени
— Движение линии: куда идут деньги
— Травмы, дисквалификации, домашний фактор

На выходе — 3 матча дня с максимальной уверенностью AI.

Без воды, без эмоций — только данные.""",

    # Day 1 — Post 2 (15:00)
    """📊 Факт дня

Команды, проигравшие 3 матча подряд дома, выигрывают 4-й домашний матч в 62% случаев.

Букмекеры это знают — именно поэтому линия на таких матчах часто сдвигается за 2-3 часа до начала.

Следить за движением линии — один из самых надёжных индикаторов. Betly отслеживает это автоматически.""",

    # Day 1 — Post 3 (21:00)
    """📈 Как читать движение линии?

Если КЭФ на победу хозяев падает с 2.10 до 1.75 за 6 часов — значит крупные игроки ставят на хозяев.

Это не гарантия, но сильный сигнал. Особенно если одновременно:
— Форма команды на подъёме
— H2H в пользу хозяев
— Тотал растёт (ожидают голы)

Betly фиксирует такие движения и учитывает их в рекомендациях.""",

    # Day 2 — Post 1 (09:00)
    """⚽ Что смотреть сегодня

Несколько интересных матчей на вечер:

— АПЛ: борьба за еврокубки — каждое очко на вес золота
— КХЛ: плей-офф на горизонте, мотивация максимальная
— ATP: грунтовый сезон в разгаре

Какие из них попадут в Топ-3 от AI — покажем в 11:00.""",

    # Day 2 — Post 2 (15:00)
    """🧠 Что такое Value Bet?

Value bet — это ставка, где реальная вероятность события выше, чем считает букмекер.

Пример: букмекер даёт КЭФ 2.50 на победу (подразумевает 40% вероятность). Но по статистике и форме вероятность — 50%.

Это и есть ценность. На дистанции такие ставки приносят прибыль, даже если отдельные проигрывают.

AI Betly ищет именно такие расхождения между линией и реальными данными.""",

    # Day 2 — Post 3 (21:00)
    """🏒 Интересная статистика по хоккею

В КХЛ средний тотал в плей-офф — 4.8 шайб за матч. В регулярке — 5.3.

Причина: в плей-офф команды играют плотнее, меньше рискуют. Тоталы снижаются, а букмекеры не всегда это учитывают в первых матчах серии.

Именно на таких нюансах можно находить ценные ставки.""",

    # Day 3 — Post 1 (09:00)
    """🏆 7 видов спорта — один бот

Betly анализирует:

⚽ Футбол — АПЛ, Ла Лига, Серия А, Бундеслига, Лига Чемпионов, РПЛ
🏒 Хоккей — КХЛ, НХЛ
🏀 Баскетбол — NBA, Евролига
🎾 Теннис — ATP, WTA (все крупные турниры)
🥊 MMA — UFC
🏐 Волейбол — Суперлига РФ
🏎 Формула 1 — все этапы

Не нужно переключаться между 10 источниками — всё в одном месте.""",

    # Day 3 — Post 2 (15:00)
    """⚠ 3 главные ошибки при анализе матчей

1. Смотреть только на таблицу. Команда на 3-м месте может быть в ужасной форме, а 8-я — на серии из 5 побед.

2. Игнорировать H2H. Некоторые команды исторически неудобны для конкретных соперников — это повторяется из сезона в сезон.

3. Не следить за линией. Движение КЭФов за 3-6 часов до матча — это "голос денег". Крупные игроки редко ошибаются.

AI Betly учитывает все три фактора одновременно.""",

    # Day 3 — Post 3 (21:00)
    """🎾 Знали ли вы?

На грунтовых кортах андердоги выигрывают на 18% чаще, чем на харде. Грунт замедляет подачу и уравнивает шансы.

Именно поэтому букмекеры дают более осторожные линии на грунтовых турнирах. А значит — меньше value для фаворитов и больше для аутсайдеров.""",

    # Day 4 — Post 1 (09:00)
    """🎯 Как устроен Охотник

Каждый день в 11:00 МСК Betly публикует Топ-3 матча дня. Для каждого:

— Уверенность AI (от 55% до 85%)
— Рекомендация: П1, П2, ТБ, ТМ или комбинация
— КЭФы от ведущих букмекеров
— Краткий разбор: почему именно этот матч

Плюс — экспресс дня из 3 матчей с общим КЭФом.

Тизер матчей — здесь, в канале. Полный разбор с рекомендациями — в боте.""",

    # Day 4 — Post 2 (16:00)
    """💰 Простое правило банк-менеджмента

Профессионалы никогда не ставят больше 3-5% банка на одно событие. Даже если уверенность 80%.

Почему? Потому что даже при 70% точности на дистанции, серия из 3-4 проигрышей — нормальное явление. Если каждая ставка — 20% банка, то 4 проигрыша подряд = минус 80% банка.

При ставке 3% и 4 проигрышах — минус 12%. Банк выдержит и восстановится на следующей серии побед.

Дисциплина важнее прогноза.""",

    # Day 5 — Post 1 (09:00)
    """🤖 Почему AI, а не человек?

Человек-аналитик в лучшем случае может качественно разобрать 5-10 матчей в день. AI Betly обрабатывает 500+ за 2 минуты.

При этом AI не подвержен:
— Эмоциям ("любимая команда точно выиграет")
— Усталости (понедельник или суббота — одинаковое качество)
— Когнитивным искажениям (недавний яркий матч не влияет на оценку)

AI не заменяет экспертизу — он усиливает её данными.""",

    # Day 5 — Post 2 (15:00)
    """📊 Тоталы: недооценённый рынок

Большинство начинающих игроков ставят на исходы: П1, Х, П2. Но рынок тоталов часто даёт больше value.

Почему: исходы зависят от множества непредсказуемых факторов (удаление, пенальти). А тоталы больше подчиняются статистике — средний тотал команды на дистанции 10-15 матчей довольно стабилен.

Betly анализирует тоталы обеих команд и сравнивает с линией букмекера.""",

    # Day 5 — Post 3 (21:00)
    """🏀 Факт: домашнее преимущество в NBA

Домашние команды в NBA выигрывают 58% матчей. Но в плей-офф этот показатель вырастает до 65%.

Причина — фан-фактор и усталость от перелётов. Особенно влияет на 4-ю и 5-ю игры серии, когда команды уже измотаны.

AI учитывает домашний фактор с разным весом для регулярки и плей-офф.""",

    # Day 6 — Post 1 (09:00)
    """⚖ КЭФы: почему у разных букмекеров разные линии?

Каждый букмекер оценивает вероятности по-своему. Разница в КЭФах между Pinnacle и Bet365 на один и тот же исход может составлять 0.10-0.30.

На одной ставке это кажется мелочью. Но на дистанции 100 ставок — это разница между прибылью и убытком.

Betly показывает КЭФы от 5 букмекеров одновременно и подсвечивает лучший вариант.""",

    # Day 6 — Post 2 (16:00)
    """⚡ LIVE-аналитика: зачем следить за матчем в реальном времени?

Статистика до матча — это прогноз. Статистика во время матча — это факт.

Если команда владеет мячом 65%, наносит 8 ударов против 2, но счёт 0:0 — вероятность гола растёт с каждой минутой. Линия на тотал больше может быть выгодной.

Betly PRO показывает LIVE-данные: удары, владение, голевые моменты — и обновляет рекомендации в реальном времени.""",

    # Day 7 — Post 1 (09:00)
    """📊 Прозрачность результатов

Каждый вечер в 22:00 МСК мы публикуем результаты дневных прогнозов.

Формат: 2/3 ✅✅❌ — сколько рекомендаций зашло из трёх.

Никакой подрисовки, никаких удалённых постов. Вся история сохраняется в боте — команда /stats покажет статистику за любой период.

Мы верим, что доверие строится на прозрачности.""",

    # Day 7 — Post 2 (15:00)
    """🏎 Формула 1: что анализирует Betly?

F1 — не только гонка, но и стратегия. Betly собирает:

— Результаты квалификации и стартовую решётку
— Историю трассы: кто доминировал в прошлые годы
— Погодные условия (дождь кардинально меняет расклад)
— Чемпионат пилотов и конструкторов

Следующий этап уже скоро — разбор будет в канале.""",

    # Day 7 — Post 3 (21:00)
    """📱 Как начать пользоваться Betly?

1. Откройте бота — @BetlyAIBot
2. Нажмите /start
3. Выберите вид спорта
4. Получайте матчи, аналитику и прогнозы

Бесплатно: матчи дня, счета, турнирные таблицы по 7 видам спорта.

PRO: AI-прогнозы Охотника, КЭФы от 5 букмекеров, LIVE-аналитика.

🎁 Промокод BETLY2026 — 7 дней PRO бесплатно.""",
    ]
    # 19 scheduled posts total (matching schedule above)
    assert len(post_texts) == 19, f"Expected 19 posts, got {len(post_texts)}"
    assert len(schedule) == 19, f"Expected 19 schedule entries, got {len(schedule)}"
    return schedule, post_texts


async def publish_pinned_post_now(bot) -> Optional[int]:
    """Publish the pinned post immediately and pin it. Returns message_id."""
    if not CHANNEL_USERNAME:
        logger.warning("CHANNEL_USERNAME not set, cannot publish pinned post")
        return None

    try:
        msg = await bot.send_message(chat_id=CHANNEL_USERNAME, text=_get_pinned_post())
        await bot.pin_chat_message(
            chat_id=CHANNEL_USERNAME,
            message_id=msg.message_id,
            disable_notification=True,
        )
        logger.info("Pinned post published and pinned (msg_id=%d)", msg.message_id)
        return msg.message_id
    except Exception:
        logger.exception("Failed to publish/pin the pinned post")
        return None


def load_predefined_posts(start_date: datetime) -> int:
    """
    Bulk-insert 19 scheduled posts starting from start_date.
    start_date should be a date in MSK (the "tomorrow" concept).
    Returns count of inserted posts. Post texts are loaded on demand and GC'd after.
    """
    import gc
    schedule, post_texts = _get_schedule_and_posts()
    count = 0
    base_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)

    try:
        for (day, hour, minute), post_text in zip(schedule, post_texts):
            publish_msk = base_date + timedelta(days=day - 1)
            publish_msk = publish_msk.replace(hour=hour, minute=minute)
            if publish_msk.tzinfo is None:
                publish_msk = publish_msk.replace(tzinfo=MSK)
            publish_utc = publish_msk.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
            add_scheduled_post(post_text, publish_utc, pinned=False)
            count += 1
    finally:
        del schedule, post_texts
        gc.collect()

    logger.info("Loaded %d predefined posts starting from %s", count, base_date.date())
    return count


def clear_unpublished_posts() -> int:
    """Delete all unpublished posts. Returns count deleted."""
    with engine.begin() as conn:
        res = conn.execute(text("DELETE FROM scheduled_posts WHERE published = FALSE"))
        return res.rowcount
