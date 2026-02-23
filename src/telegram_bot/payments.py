# src/telegram_bot/payments.py
"""
Payment system: multi-tier tariffs (RUB via ЮKassa + Telegram Stars).

Tariffs:
  - week   (7 дней)   — 299₽  / 150 Stars
  - month  (30 дней)  — 799₽  / 400 Stars
  - season (180 дней) — 3990₽ / 2000 Stars

Flow: kb_tariffs → send_invoice → pre_checkout → successful_payment → grant_pro
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PAYMENTS_PROVIDER_TOKEN = (
    os.getenv("YUKASSA_PROVIDER_TOKEN")
    or os.getenv("PAYMENTS_PROVIDER_TOKEN")
    or ""
).strip()

# If no provider token → we use Telegram Stars (XTR currency, no юрлицо needed)
def _use_stars() -> bool:
    return not PAYMENTS_PROVIDER_TOKEN


# ---------------------------------------------------------------------------
# Tariffs
# ---------------------------------------------------------------------------
@dataclass
class Tariff:
    key: str          # "week" / "month" / "season"
    label: str        # display name
    days: int
    price_rub: int    # in rubles
    price_stars: int   # Telegram Stars amount
    emoji: str


TARIFFS: Dict[str, Tariff] = {
    "week": Tariff(
        key="week", label="Неделя", days=7,
        price_rub=299, price_stars=150, emoji="7️⃣",
    ),
    "month": Tariff(
        key="month", label="Месяц", days=30,
        price_rub=799, price_stars=400, emoji="30️⃣",
    ),
    "season": Tariff(
        key="season", label="Сезон", days=180,
        price_rub=3990, price_stars=2000, emoji="🏆",
    ),
}


# ---------------------------------------------------------------------------
# UI: tariff selection screen
# ---------------------------------------------------------------------------
def tariff_text() -> str:
    return (
        "🌟 Betly PRO — AI-аналитика спорта\n\n"
        "Что включено в PRO:\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎯 Охотник — Топ-3 события дня каждое утро\n"
        "📊 Экспресс дня — собранный AI экспресс\n"
        "⚡ LIVE PRO — аналитика в реальном времени\n"
        "📈 Расширенная статистика и H2H\n"
        "🔔 Push-уведомления о результатах\n\n"
        "Тарифы:\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📅 Неделя — 299 ₽\n"
        "📅 Месяц — 799 ₽ (экономия 25%)\n"
        "📅 Сезон — 3 990 ₽ (экономия 17%)\n\n"
        "✅ Отмена в любой момент\n"
        "✅ Оплата картой прямо в Telegram"
    )


def kb_tariffs() -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton("📅 Неделя — 299 ₽", callback_data="PRO:week")],
        [InlineKeyboardButton("📅 Месяц — 799 ₽ ⭐ Популярный", callback_data="PRO:month")],
        [InlineKeyboardButton("📅 Сезон — 3 990 ₽ 🔥 -17%", callback_data="PRO:season")],
        [InlineKeyboardButton("⬅️ Назад в меню", callback_data="BACK:MENU")],
    ]
    return InlineKeyboardMarkup(rows)


def pro_stub_text(tariff_key: str) -> str:
    """Временная заглушка при нажатии на тариф (до подключения ЮKassa)."""
    tariff = TARIFFS.get(tariff_key)
    if not tariff:
        name = "PRO"
        price = ""
    else:
        name = f"PRO {tariff.label}"
        price = f" ({tariff.price_rub} ₽)"
    return (
        "💳 Подключение оплаты в процессе.\n\n"
        f"Тариф: {name}{price}\n"
        "Оплата станет доступна в ближайшее время!\n\n"
        "Хотите получить бесплатный пробный период на 3 дня?"
    )


def kb_pro_stub() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 Попробовать бесплатно 3 дня", callback_data="PRO:TRIAL")],
        [InlineKeyboardButton("⬅️ Назад к тарифам", callback_data="MENU:PREMIUM")],
    ])


# ---------------------------------------------------------------------------
# Invoice sending
# ---------------------------------------------------------------------------
async def send_invoice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tariff_key: str,
) -> None:
    """Send Telegram payment invoice for selected tariff."""
    tariff = TARIFFS.get(tariff_key)
    if not tariff:
        await update.effective_message.reply_text("Неизвестный тариф.")
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    use_stars = _use_stars()

    title = f"PRO {tariff.label} ({tariff.days} дн.)"
    description = "Полный доступ к PRO-функциям: Охотник, LIVE PRO, расширенная аналитика."
    payload = f"pro_{tariff.key}_{user_id}"

    if use_stars:
        # Telegram Stars — currency XTR, provider_token empty string
        prices = [LabeledPrice(label=title, amount=tariff.price_stars)]
        await context.bot.send_invoice(
            chat_id=chat_id,
            title=title,
            description=description,
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=prices,
        )
    else:
        # ЮKassa (RUB) — amount in kopecks
        prices = [LabeledPrice(label=title, amount=tariff.price_rub * 100)]

        # Чек 54-ФЗ (обязательно для ИП/ООО с ЮKassa)
        import json
        provider_data = json.dumps({
            "receipt": {
                "items": [
                    {
                        "description": f"PRO {tariff.label} — AI Sports Analytics",
                        "quantity": "1.00",
                        "amount": {
                            "value": f"{tariff.price_rub:.2f}",
                            "currency": "RUB",
                        },
                        "vat_code": 1,  # без НДС (УСН)
                        "payment_mode": "full_payment",
                        "payment_subject": "service",
                    }
                ]
            }
        })

        await context.bot.send_invoice(
            chat_id=chat_id,
            title=title,
            description=description,
            payload=payload,
            provider_token=PAYMENTS_PROVIDER_TOKEN,
            currency="RUB",
            prices=prices,
            need_name=False,
            need_phone_number=False,
            need_email=True,               # email для чека по 54-ФЗ
            send_email_to_provider=True,    # передать email в ЮKassa
            need_shipping_address=False,
            is_flexible=False,
            provider_data=provider_data,
        )

    logger.info("Invoice sent: user=%s tariff=%s stars=%s", user_id, tariff_key, use_stars)


# ---------------------------------------------------------------------------
# Pre-checkout validation
# ---------------------------------------------------------------------------
async def handle_pre_checkout(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Validate pre-checkout query — always approve if payload is valid."""
    q = update.pre_checkout_query
    if not q:
        return

    payload = q.invoice_payload or ""
    # Payload format: pro_<tariff_key>_<user_id>
    if not payload.startswith("pro_"):
        await q.answer(ok=False, error_message="Некорректный платёж. Попробуй ещё раз.")
        return

    parts = payload.split("_", 2)
    tariff_key = parts[1] if len(parts) > 1 else ""
    if tariff_key not in TARIFFS:
        await q.answer(ok=False, error_message="Неизвестный тариф.")
        return

    await q.answer(ok=True)


# ---------------------------------------------------------------------------
# Successful payment
# ---------------------------------------------------------------------------
async def handle_successful_payment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Process successful payment: grant PRO + log + notify."""
    msg = update.message
    if not msg or not msg.successful_payment:
        return

    user_id = update.effective_user.id if update.effective_user else 0
    sp = msg.successful_payment
    payload = sp.invoice_payload or ""

    # Parse payload
    parts = payload.split("_", 2)
    tariff_key = parts[1] if len(parts) > 1 else "month"
    tariff = TARIFFS.get(tariff_key)
    if not tariff:
        tariff = TARIFFS["month"]  # fallback

    # Grant PRO
    try:
        from ..pro_db import grant_pro
        ok = grant_pro(user_id, days=tariff.days)
        if not ok:
            logger.error("grant_pro failed for user=%s after payment", user_id)
            try:
                from ..alerting import send_alert
                await send_alert(
                    "CRITICAL", "payments.grant_pro",
                    RuntimeError(f"grant_pro returned False for user={user_id}"),
                    user_id=user_id,
                    context={"tariff": tariff.key, "days": tariff.days},
                )
            except Exception:
                pass
    except Exception as _gp_err:
        logger.exception("grant_pro error after payment user=%s", user_id)
        try:
            from ..alerting import send_alert
            await send_alert(
                "CRITICAL", "payments.grant_pro",
                _gp_err,
                user_id=user_id,
                context={"tariff": tariff.key, "days": tariff.days, "payload": payload},
            )
        except Exception:
            pass

    # Determine payment info for logging
    use_stars = _use_stars()
    amount = tariff.price_stars if use_stars else tariff.price_rub
    currency = "XTR" if use_stars else "RUB"

    # Log payment to DB
    try:
        _log_payment(
            user_id=user_id,
            tariff_key=tariff.key,
            amount=amount,
            currency=currency,
            payment_method="stars" if use_stars else "yukassa",
            telegram_payment_charge_id=sp.telegram_payment_charge_id or "",
            provider_payment_charge_id=sp.provider_payment_charge_id or "",
        )
    except Exception:
        logger.exception("Payment log failed user=%s", user_id)

    # Get premium_until for display
    until_str = "—"
    try:
        from ..pro_db import get_pro_status
        status = get_pro_status(user_id)
        pu = status.get("premium_until")
        if pu:
            if isinstance(pu, datetime):
                until_str = pu.strftime("%d.%m.%Y")
            elif isinstance(pu, str):
                until_str = pu[:10]
    except Exception:
        pass

    price_label = f"{tariff.price_stars} Stars" if use_stars else f"{tariff.price_rub}₽"

    # Success message with navigation buttons
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Охотник", callback_data="MENU:HUNTER")],
        [InlineKeyboardButton("📊 Анализ матчей", callback_data="BACK:MATCHES_MENU")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="BACK:MENU")],
    ])
    await msg.reply_text(
        f"✅ PRO активирован!\n\n"
        f"Тариф: {tariff.emoji} {tariff.label} ({tariff.days} дн.)\n"
        f"Сумма: {price_label}\n"
        f"PRO активен до: {until_str}\n\n"
        "Что теперь доступно:\n"
        "🎯 Охотник — Топ-3 придёт завтра утром\n"
        "🔥 LIVE PRO — уже доступен в анализе матчей\n\n"
        "💡 Совет: зайди в Охотник чтобы увидеть сегодняшний Топ-3",
        reply_markup=kb,
    )
    logger.info(
        "Payment OK: user=%s tariff=%s amount=%s %s",
        user_id, tariff.key, amount, currency,
    )

    # Create receipt for tax compliance (only for RUB payments via ЮKassa)
    receipt_url: Optional[str] = None
    if not use_stars:
        try:
            from ..receipts import create_receipt
            _username = getattr(update.effective_user, "username", None) or ""
            receipt_url = create_receipt(
                amount=float(tariff.price_rub),
                service_name=f"PRO подписка Betly ({tariff.label}, {tariff.days} дн.)",
                client_name=f"@{_username}" if _username else None,
            )
            await msg.reply_text(f"🧾 Ваш чек: {receipt_url}")
            logger.info("Receipt sent: user=%s url=%s", user_id, receipt_url)
        except Exception as _rcpt_err:
            logger.exception("Receipt creation failed user=%s", user_id)
            try:
                from ..alerting import send_alert
                await send_alert(
                    "WARNING", "payments.receipt", _rcpt_err,
                    user_id=user_id,
                    context={"amount": tariff.price_rub, "tariff": tariff.key},
                )
            except Exception:
                pass

    # Notify admin about new payment
    try:
        admin_id = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
        if admin_id and context.bot:
            username = getattr(update.effective_user, "username", None) or ""
            admin_text = (
                f"💰 Новая оплата!\n"
                f"User: {user_id} (@{username})\n"
                f"Тариф: {tariff.emoji} {tariff.label} ({tariff.days} дн.)\n"
                f"Сумма: {price_label}\n"
                f"Метод: {'Stars' if use_stars else 'ЮKassa'}\n"
                f"Telegram charge: {sp.telegram_payment_charge_id or '—'}"
            )
            if receipt_url:
                admin_text += f"\n🧾 Чек: {receipt_url}"
            await context.bot.send_message(chat_id=admin_id, text=admin_text)
    except Exception:
        logger.debug("Failed to notify admin about payment user=%s", user_id)


# ---------------------------------------------------------------------------
# Payment logging (DB)
# ---------------------------------------------------------------------------
def _log_payment(
    user_id: int,
    tariff_key: str,
    amount: int,
    currency: str,
    payment_method: str,
    telegram_payment_charge_id: str = "",
    provider_payment_charge_id: str = "",
) -> None:
    """Save payment record to the payments table."""
    from sqlalchemy import text as sa_text
    from ..db import SessionLocal

    session = SessionLocal()
    try:
        session.exec(
            sa_text("""
                INSERT INTO payments (
                    user_id, tariff, amount, currency, payment_method,
                    telegram_charge_id, provider_charge_id, status, created_at
                ) VALUES (
                    :uid, :tariff, :amount, :currency, :method,
                    :tg_charge, :prov_charge, 'completed', NOW()
                )
            """),
            params={
                "uid": user_id,
                "tariff": tariff_key,
                "amount": amount,
                "currency": currency,
                "method": payment_method,
                "tg_charge": telegram_payment_charge_id,
                "prov_charge": provider_payment_charge_id,
            },
        )
        session.commit()
        logger.info("Payment logged: user=%s tariff=%s", user_id, tariff_key)
    except Exception:
        session.rollback()
        logger.exception("_log_payment failed")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Subscription expiry checker (called from cron in service.py)
# ---------------------------------------------------------------------------
async def check_expired_subscriptions(bot) -> None:
    """
    Check for expiring/expired PRO subscriptions:
    1. Notify users 24h before expiry
    2. Deactivate expired subscriptions
    """
    from sqlalchemy import text as sa_text
    from ..db import SessionLocal

    session = SessionLocal()
    try:
        # 1. Notify users expiring within 24 hours (but not yet expired)
        rows = session.exec(
            sa_text("""
                SELECT tg_user_id, premium_until
                FROM users
                WHERE is_premium = TRUE
                  AND premium_until IS NOT NULL
                  AND premium_until > NOW()
                  AND premium_until <= NOW() + INTERVAL '24 hours'
                  AND tg_user_id IS NOT NULL
            """)
        ).all()

        for row in rows:
            try:
                uid = row[0]
                until = row[1]
                until_str = until.strftime("%d.%m.%Y %H:%M") if isinstance(until, datetime) else str(until)[:16]
                if bot:
                    await bot.send_message(
                        chat_id=uid,
                        text=(
                            f"⏰ Твой PRO доступ истекает {until_str} (МСК)\n\n"
                            "Продли подписку, чтобы не потерять доступ к Охотнику и LIVE PRO.\n"
                            "Нажми /start → PRO режим"
                        ),
                    )
                    logger.info("Expiry notification sent to user=%s", uid)
            except Exception:
                logger.debug("Failed to notify user=%s about expiry", row[0])

        # 2. Deactivate expired subscriptions
        result = session.exec(
            sa_text("""
                UPDATE users
                SET is_premium = FALSE,
                    updated_at = NOW()
                WHERE is_premium = TRUE
                  AND premium_until IS NOT NULL
                  AND premium_until <= NOW()
            """)
        )
        session.commit()

        affected = result.rowcount if hasattr(result, 'rowcount') else 0
        if affected:
            logger.info("Deactivated %d expired subscriptions", affected)

    except Exception:
        session.rollback()
        logger.exception("check_expired_subscriptions failed")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Referral bonus
# ---------------------------------------------------------------------------
REFERRAL_BONUS_DAYS = 3  # бонус за реферала (и приглашённому, и пригласившему)

async def process_referral(
    new_user_id: int,
    referrer_id: int,
    bot=None,
) -> None:
    """
    Process referral: give bonus days to both referrer and new user.
    Called from handle_start when ref_ deep link is detected.
    """
    if new_user_id == referrer_id:
        return  # нельзя пригласить себя

    from sqlalchemy import text as sa_text
    from ..db import SessionLocal

    session = SessionLocal()
    try:
        # Check if referral already recorded
        exists = session.exec(
            sa_text("""
                SELECT 1 FROM referrals
                WHERE referrer_id = :ref AND referred_id = :new
                LIMIT 1
            """),
            params={"ref": referrer_id, "new": new_user_id},
        ).first()

        if exists:
            return  # already processed

        # Record referral
        session.exec(
            sa_text("""
                INSERT INTO referrals (referrer_id, referred_id, created_at)
                VALUES (:ref, :new, NOW())
            """),
            params={"ref": referrer_id, "new": new_user_id},
        )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("process_referral: DB error")
        return
    finally:
        session.close()

    # Grant bonus days to both
    try:
        from ..pro_db import grant_pro, is_pro, get_pro_status

        # Referrer: extend or grant
        grant_pro(referrer_id, days=REFERRAL_BONUS_DAYS)

        # New user: extend or grant
        grant_pro(new_user_id, days=REFERRAL_BONUS_DAYS)

        # Notify referrer
        if bot:
            try:
                await bot.send_message(
                    chat_id=referrer_id,
                    text=(
                        f"🎉 По твоей ссылке зарегистрирован новый пользователь!\n"
                        f"+{REFERRAL_BONUS_DAYS} дня PRO в подарок."
                    ),
                )
            except Exception:
                logger.debug("Failed to notify referrer=%s", referrer_id)

    except Exception:
        logger.exception("process_referral: grant_pro failed")


def get_referral_link(user_id: int, bot_username: str) -> str:
    """Generate referral deep link for a user."""
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


def get_referral_count(user_id: int) -> int:
    """Count how many users were referred by this user."""
    from sqlalchemy import text as sa_text
    from ..db import SessionLocal

    session = SessionLocal()
    try:
        count = session.exec(
            sa_text("SELECT COUNT(*) FROM referrals WHERE referrer_id = :uid"),
            params={"uid": user_id},
        ).scalar()
        return int(count or 0)
    except Exception:
        logger.exception("get_referral_count failed")
        return 0
    finally:
        session.close()
