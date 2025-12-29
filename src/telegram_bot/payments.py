# src/telegram_bot/payments.py
from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from typing import Optional

from telegram import InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, Update
from telegram.ext import ContextTypes

from ..user_store import get_user, activate_premium

logger = logging.getLogger(__name__)

PAYMENTS_PROVIDER_TOKEN = (os.getenv("PAYMENTS_PROVIDER_TOKEN") or "").strip()
PREMIUM_PRICE_RUB = int((os.getenv("PREMIUM_PRICE_RUB") or "299").strip())
PREMIUM_DAYS = int((os.getenv("PREMIUM_DAYS") or "30").strip())
PREMIUM_CURRENCY = (os.getenv("PREMIUM_CURRENCY") or "RUB").strip().upper()
PREMIUM_PAYLOAD = (os.getenv("PREMIUM_PAYLOAD") or "premium_month").strip()

TRIAL_LIVE_ENABLED = (os.getenv("TRIAL_LIVE_ENABLED") or "1").strip() == "1"

def kb_paywall(user_id: int) -> InlineKeyboardMarkup:
    u = get_user(user_id)
    rows = []

    rows.append([InlineKeyboardButton("💳 Оформить Premium", callback_data="PAY:PREMIUM")])

    if TRIAL_LIVE_ENABLED and not u.trial_live_used:
        rows.append([InlineKeyboardButton("🎁 1 пробный LIVE", callback_data="PAY:TRIAL_LIVE")])

    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="BACK:MATCHES")])
    return InlineKeyboardMarkup(rows)

def paywall_text(user_id: int) -> str:
    u = get_user(user_id)
    lines = []
    lines.append("🟢 LIVE-обзор — Premium")
    lines.append("")
    lines.append("Что входит в Premium:")
    lines.append("• LIVE-логика движения линии без чисел и призывов")
    lines.append("• Быстрые обновления по кнопке 🔄")
    lines.append("• Доступ к LIVE по всем матчам")
    lines.append("")
    if TRIAL_LIVE_ENABLED and not u.trial_live_used:
        lines.append("🎁 Доступен 1 пробный LIVE.")
    lines.append("Дисклеймер: аналитика, не рекомендация.")
    return "\n".join(lines)

async def send_premium_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not PAYMENTS_PROVIDER_TOKEN:
        await update.effective_message.reply_text("Оплата временно недоступна (PAYMENTS_PROVIDER_TOKEN не задан).")
        return

    chat_id = update.effective_chat.id
    title = "Premium доступ на 30 дней"
    description = "Открывает LIVE-обзор и расширенные функции."
    payload = PREMIUM_PAYLOAD

    prices = [LabeledPrice(label=title, amount=PREMIUM_PRICE_RUB * 100)]  # в копейках

    await context.bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=description,
        payload=payload,
        provider_token=PAYMENTS_PROVIDER_TOKEN,
        currency=PREMIUM_CURRENCY,
        prices=prices,
        need_name=False,
        need_phone_number=False,
        need_email=False,
        need_shipping_address=False,
        is_flexible=False,
    )

async def handle_pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.pre_checkout_query
    if not q:
        return

    # валидируем payload
    if q.invoice_payload != PREMIUM_PAYLOAD:
        await q.answer(ok=False, error_message="Некорректный payload. Попробуй ещё раз.")
        return

    await q.answer(ok=True)

async def handle_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.successful_payment:
        return

    user_id = update.effective_user.id
    sp = msg.successful_payment

    if sp.invoice_payload != PREMIUM_PAYLOAD:
        await msg.reply_text("Платёж получен, но payload не распознан. Напиши в поддержку.")
        return

    u = activate_premium(user_id, PREMIUM_DAYS)
    until = u.premium_until.isoformat() if u.premium_until else "—"

    await msg.reply_text(
        "✅ Оплата прошла!\n"
        f"Premium активирован до: {until}\n\n"
        "Теперь можешь пользоваться 🟢 LIVE.",
    )
