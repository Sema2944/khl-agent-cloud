diff --git a/src/parsing.py b/src/parsing.py
index 1111111..2222222 100644
--- a/src/parsing.py
+++ b/src/parsing.py
@@ -1,40 +1,43 @@
 # src/parsing.py
 from __future__ import annotations

 import logging
 import os
 import re
 from contextlib import contextmanager
 from datetime import datetime, timedelta
-from typing import Optional, List, Tuple
+from typing import Optional, List, Tuple

 from sqlmodel import Session, SQLModel, Field, select

 from .db import get_session
 from . import bets_db
-from .hockey_logic import khl_today_text_from_winline

 logger = logging.getLogger(__name__)

+# ---------------------------------------------------------------------
+# ВНЕШНИЕ API (Winline/OddsAPI/etc) ОТКЛЮЧЕНЫ В MVP
+# ---------------------------------------------------------------------
+# Render может не резолвить/блокировать букмекеров → чтобы не было таймаутов/падений
+DISABLE_EXTERNAL_APIS = (os.getenv("DISABLE_EXTERNAL_APIS") or "true").strip().lower() in ("1", "true", "yes", "on")
+
 # -----------------------------
 # Экспертная стратегия (ENV fallback)
 # -----------------------------
 EXPERT_STRATEGY_TEXT = (os.getenv("EXPERT_STRATEGY_TEXT") or "").strip()
 EXPERT_STRATEGY_DATE = (os.getenv("EXPERT_STRATEGY_DATE") or "").strip()  # YYYY-MM-DD
 ADMIN_TELEGRAM_ID = int((os.getenv("ADMIN_TELEGRAM_ID") or "0").strip() or 0)

+# -----------------------------
+# Timezone: стратегия "на сегодня" по МСК
+# -----------------------------
+MSK_OFFSET_SECONDS = 3 * 60 * 60  # UTC+3
+def _today_msk_iso() -> str:
+    return (datetime.utcnow() + timedelta(seconds=MSK_OFFSET_SECONDS)).date().isoformat()
+
 # -----------------------------
 # DB model: Expert Strategy (stored, updatable by admin)
 # -----------------------------
 class ExpertStrategy(SQLModel, table=True):
     __tablename__ = "expert_strategy"
     id: Optional[int] = Field(default=None, primary_key=True)
     date: str = Field(index=True)  # YYYY-MM-DD
     text: str
     created_at: datetime = Field(default_factory=datetime.utcnow)
     updated_at: datetime = Field(default_factory=datetime.utcnow)
     updated_by: int = Field(default=0, index=True)

@@ -109,22 +112,24 @@ def _format_week_report(bets: List[bets_db.Bet]) -> str:
 # -----------------------------
 # Эксперт: хранение/чтение
 # -----------------------------
 def _get_strategy_from_db(session: Session, date_str: str) -> Optional[ExpertStrategy]:
     st = select(ExpertStrategy).where(ExpertStrategy.date == date_str).order_by(ExpertStrategy.updated_at.desc())
     return session.exec(st).first()

 def _format_expert_strategy() -> str:
     """
     Показывает стратегию эксперта на сегодня:
     1) сначала пробуем из БД (если админ обновлял)
     2) потом ENV fallback
     """
-    today = datetime.utcnow().date().isoformat()
+    today = _today_msk_iso()

     db_text = None
     db_date = today
     with db_session() as session:
         row = _get_strategy_from_db(session, today)
         if row and row.text:
             db_text = row.text
             db_date = row.date

     text = db_text or EXPERT_STRATEGY_TEXT
-    date_label = db_date if db_text else (EXPERT_STRATEGY_DATE or today)
+    date_label = db_date if db_text else (EXPERT_STRATEGY_DATE or today)

     if not text:
         return (
             "👤 *Стратегия эксперта на сегодня*\n"
             "_Пока не опубликована._\n\n"
             "Если ты админ — обнови командой:\n"
             "`админ стратегия: <текст>`"
         )

@@ -165,7 +170,7 @@ def _try_admin_update_strategy(user_id: int, raw_text: str) -> Tuple[bool, str]:
     if not new_text:
         return False, "Пустой текст стратегии."

-    today = datetime.utcnow().date().isoformat()
+    today = _today_msk_iso()

     with db_session() as session:
         row = _get_strategy_from_db(session, today)
         if row is None:
             row = ExpertStrategy(
                 date=today,
                 text=new_text,
                 updated_by=user_id,
                 updated_at=datetime.utcnow(),
             )
             session.add(row)
         else:
             row.text = new_text
             row.updated_by = user_id
             row.updated_at = datetime.utcnow()
             session.add(row)
         session.commit()

     return True, (
         "✅ Стратегия обновлена и сохранена в БД.\n\n"
         "Теперь всем пользователям команда `стратегия` покажет обновлённый текст.\n"
         "ENV/Deploy не нужен."
     )

@@ -173,6 +178,87 @@ def _try_admin_update_strategy(user_id: int, raw_text: str) -> Tuple[bool, str]:
 # -----------------------------
 # Линия и нормализация (MVP демо)
 # -----------------------------
 def _normalize_demo_markets() -> list[dict]:
@@ -222,6 +308,68 @@ def _format_line(match_id: str) -> str:
     lines.append("_Дисклеймер: линия показана для объяснения рынков. Не является рекомендацией._")
     return "\n".join(lines)

+def _format_sports_menu() -> str:
+    return (
+        "🏟 *Матчи сегодня*\n\n"
+        "Выбери спорт кнопкой ниже или напиши командой:\n"
+        "• `матчи хоккей`\n"
+        "• `матчи футбол`\n"
+        "• `матчи баскетбол`\n"
+        "• `матчи теннис`\n"
+        "• `матчи киберспорт`\n"
+    )
+
+def _format_hockey_today() -> str:
+    # В MVP внешний источник выключен → показываем демо (стабильно, быстро)
+    return (
+        "🏒 *Хоккей — матчи сегодня (MVP/демо)*\n\n"
+        "КХЛ:\n"
+        "1) СКА — ЦСКА (id: demo_khl_123456)\n\n"
+        "НХЛ:\n"
+        "1) Rangers — Devils (id: demo_nhl_987654)\n\n"
+        "Чтобы получить линию:\n"
+        "• `линия <id>`\n"
+        "Чтобы получить AI-разбор:\n"
+        "• `аналитика <id>`\n"
+        "Стратегия эксперта:\n"
+        "• `стратегия`\n"
+    )
+
+def _format_generic_today(sport_name: str) -> str:
+    # Заглушка под будущие адаптеры OddsAPI/других API
+    return (
+        f"📅 *{sport_name.title()} — матчи сегодня (MVP/демо)*\n\n"
+        "Пока источники линий отключены, показываю демо-режим.\n"
+        "Дальше подключим агрегатор (OddsAPI или другой) через адаптер.\n\n"
+        "Команды:\n"
+        "• `аналитика <вопрос>`\n"
+        "• `стратегия`\n"
+    )
+
+def _parse_matches_command(norm: str, raw: str) -> Tuple[bool, str]:
+    """
+    Команды:
+    - "матчи сегодня"
+    - "матчи хоккей/футбол/баскетбол/теннис/киберспорт"
+    """
+    if norm in {"матчи сегодня", "матчи", "сегодня матчи"}:
+        return True, _format_sports_menu()
+
+    if norm.startswith("матчи"):
+        tail = raw.split("матчи", 1)[1].strip().lower()
+        if not tail:
+            return True, _format_sports_menu()
+
+        if "хоккей" in tail:
+            return True, _format_hockey_today()
+        if "футбол" in tail:
+            return True, _format_generic_today("футбол")
+        if "баскетбол" in tail:
+            return True, _format_generic_today("баскетбол")
+        if "теннис" in tail:
+            return True, _format_generic_today("теннис")
+        if "кибер" in tail:
+            return True, _format_generic_today("киберспорт")
+
+        return True, _format_sports_menu()
+
+    return False, ""
+
 # -----------------------------
 # AI аналитика (MVP)
 # -----------------------------
 async def ai_analyze(user_id: int, prompt: str) -> str:
@@ -296,6 +444,12 @@ async def run_dialog_agent(user_id: int, message: str) -> str:
     # 0.1) Экспертная стратегия
     if norm in {"стратегия", "эксперт", "эксперт сегодня", "стратегия сегодня"} or norm.startswith("стратегия"):
         return _format_expert_strategy()
+
+    # 0.15) Матчи сегодня (новая главная точка входа)
+    ok, text = _parse_matches_command(norm, text_raw)
+    if ok:
+        return text

     # 0.2) Линия по матчу: "линия <id>"
     if norm.startswith("линия"):
         body = text_raw.split("линия", 1)[1].strip(" :\n\t")
         if not body:
             return "Напиши так: `линия <id>`"
         return _format_line(body)

@@ -350,27 +504,11 @@ async def run_dialog_agent(user_id: int, message: str) -> str:
         last_week_bets = [b for b in all_bets if b.created_at >= week_ago]
         return _format_week_report(last_week_bets)

-    # 5) КХЛ сегодня
-    if "кхл сегодня" in norm or "кхл на сегодня" in norm:
-        try:
-            text = await khl_today_text_from_winline()
-            if not text:
-                return "Пока не вижу линию КХЛ на сегодня. Попробуй чуть позже."
-            return text
-        except Exception as e:
-            logger.exception("khl_today_text_from_winline failed: %s", e)
-            return (
-                "🏒 Матчи КХЛ на сегодня:\n\n"
-                "1) СКА — ЦСКА (id: demo_khl_123456)\n\n"
-                "Чтобы получить линию, напиши: `линия <id>`"
-            )
+    # 5) Старое "КХЛ сегодня" — оставим алиасом, чтобы люди не ломались
+    if "кхл сегодня" in norm or "кхл на сегодня" in norm:
+        return _format_hockey_today()

@@ -440,13 +578,14 @@ async def run_dialog_agent(user_id: int, message: str) -> str:
     help_text = (
         "Я понимаю команды:\n\n"
         "• `профиль` — статистика и банк\n"
         "• `состояние банка` — текущий банк\n"
         "• `мой банк 100000` — установить банк\n"
-        "• `КХЛ сегодня` — матчи на сегодня\n"
+        "• `матчи сегодня` — выбор спорта и матчи (MVP)\n"
         "• `линия <id>` — рынки/коэффициенты (MVP)\n"
         "• `аналитика <id/вопрос>` — AI аналитика (MVP)\n"
         "• `стратегия` — стратегия эксперта на сегодня\n"
         "• `отчёт за неделю` — отчёт по ставкам\n"
         "• `разбор моих рынков` — базовый разбор рынков\n\n"
         "_Дисклеймер: сервис даёт аналитику, а не рекомендации к ставкам._"
     )
     return help_text
