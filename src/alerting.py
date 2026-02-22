# src/alerting.py
"""
Система оповещений об ошибках — отправка уведомлений админу в Telegram.

Уровни:
  CRITICAL — всегда отправлять (cooldown=0)
  ERROR    — макс 1 раз в 5 мин (дедупликация по ключу)
  WARNING  — макс 1 раз в час
"""
from __future__ import annotations

import datetime
import logging
import os
import time
from collections import defaultdict
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

ADMIN_USER_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "5027679117"))

ALERT_COOLDOWNS = {
    "CRITICAL": 0,
    "ERROR": 300,
    "WARNING": 3600,
}


class AlertManager:
    def __init__(self, bot):
        self.bot = bot
        self._last_sent: Dict[str, float] = {}
        self._counts: Dict[str, int] = defaultdict(int)

    async def alert(
        self,
        level: str,
        where: str,
        error: Exception,
        user_id: Optional[int] = None,
        username: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        key = f"{type(error).__name__}:{where}"
        cooldown = ALERT_COOLDOWNS.get(level, 300)

        now = time.time()
        self._counts[key] += 1
        last = self._last_sent.get(key, 0)

        if cooldown > 0 and (now - last) < cooldown:
            return

        count = self._counts.pop(key, 1)
        self._last_sent[key] = now

        emoji = {"CRITICAL": "\U0001f6a8", "ERROR": "\u26a0\ufe0f", "WARNING": "\U0001f7e1"}.get(level, "\u2139\ufe0f")

        parts = [
            f"{emoji} <b>{level}: BETLY</b>",
            "",
            f"\U0001f4cd <b>Где:</b> {where}",
        ]

        if user_id:
            user_str = f"@{username} " if username else ""
            parts.append(f"\U0001f464 <b>Юзер:</b> {user_str}(ID: {user_id})")

        parts.extend([
            f"\u26a0\ufe0f <b>Тип:</b> {type(error).__name__}",
            f"\U0001f4ac <b>Текст:</b> {str(error)[:500]}",
        ])

        if context:
            ctx_str = "\n".join(f"  {k}: {v}" for k, v in context.items())
            parts.append(f"\n\U0001f4ca <b>Контекст:</b>\n{ctx_str}")

        if count > 1:
            cd_min = max(1, cooldown // 60)
            parts.append(f"\n\U0001f504 <b>Повторов:</b> {count} раз за {cd_min} мин")

        now_str = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        parts.append(f"\n\U0001f550 {now_str}")

        text = "\n".join(parts)

        try:
            await self.bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=text,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error("Failed to send alert to admin: %s", e)

    async def alert_critical(self, where: str, error: Exception, **kwargs):
        await self.alert("CRITICAL", where, error, **kwargs)

    async def alert_error(self, where: str, error: Exception, **kwargs):
        await self.alert("ERROR", where, error, **kwargs)

    async def alert_warning(self, where: str, error: Exception, **kwargs):
        await self.alert("WARNING", where, error, **kwargs)


# Глобальный instance
_alert_manager: Optional[AlertManager] = None


def init_alerting(bot) -> None:
    global _alert_manager
    _alert_manager = AlertManager(bot)
    logger.info("AlertManager initialized (admin=%s)", ADMIN_USER_ID)


async def send_alert(level: str, where: str, error: Exception, **kwargs) -> None:
    if _alert_manager:
        try:
            await _alert_manager.alert(level, where, error, **kwargs)
        except Exception:
            logger.exception("send_alert itself failed")


async def send_startup_message(bot) -> None:
    try:
        await bot.send_message(
            chat_id=ADMIN_USER_ID,
            text="\u2705 Betly запущен и работает!",
        )
    except Exception:
        logger.exception("Failed to send startup message")
