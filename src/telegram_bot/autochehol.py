from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

AUTO_STATE_KEY = "auto_state"
AUTO_ORDER_KEY = "auto_order"

STATE_MENU = "menu"
STATE_ORDER_STYLE = "order_style"
STATE_ORDER_MATERIAL = "order_material"
STATE_ORDER_COLOR = "order_color"
STATE_ORDER_INSERT = "order_insert"
STATE_ORDER_OPTIONS = "order_options"
STATE_ORDER_PAYMENT = "order_payment"
STATE_ORDER_CONFIRM = "order_confirm"
STATE_SPECIALIST_DETAILS = "specialist_details"
STATE_SPECIALIST_PHONE = "specialist_phone"
STATE_INFO_TOPIC = "info_topic"
STATE_MANAGER_TOPIC = "manager_topic"
STATE_MANAGER_PHONE = "manager_phone"
STATE_DECLINE_REASON = "decline_reason"
STATE_DECLINE_OTHER = "decline_other"


@dataclass
class OrderDraft:
    style_id: Optional[str] = None
    material_id: Optional[str] = None
    color_id: Optional[str] = None
    insert_type_id: Optional[str] = None
    options: List[str] = field(default_factory=list)
    payment_id: Optional[str] = None


STYLE_IDS = [str(i) for i in range(1, 23)]
MATERIALS = {
    "oregon": "Oregon",
    "canyon": "Каньон",
    "dakota": "Dakota",
}
COLOR_MAP = {
    "oregon": [str(i) for i in range(1, 11)],
    "canyon": [str(i) for i in range(1, 5)],
    "dakota": [str(i) for i in range(1, 5)],
}
INSERT_TYPES = {
    "perf": "Перфорация",
    "smooth": "Гладкая",
}
OPTIONS = {
    "1": "Опция 1",
    "2": "Опция 2",
    "3": "Опция 3",
    "4": "Опция 4",
    "5": "Опция 5",
    "6": "Опция 6",
}
PAYMENTS = {
    "1": "Наличными/перевод",
    "2": "Карта онлайн",
    "3": "Оплата при получении",
    "4": "Рассрочка",
}
INFO_TOPICS = {
    "materials": "Материалы",
    "delivery": "Оплата и доставка",
    "warranty": "Гарантия и срок службы",
    "pricing": "Из чего цена",
    "install": "Самостоятельная установка",
}
DECLINE_REASONS = {
    "expensive": "Дорого",
    "missing": "Не нашли вариант",
    "browsing": "Просто смотрю",
    "other": "Другое",
}


def _order_from_context(context: ContextTypes.DEFAULT_TYPE) -> OrderDraft:
    data = context.user_data.get(AUTO_ORDER_KEY)
    if isinstance(data, OrderDraft):
        return data
    draft = OrderDraft()
    context.user_data[AUTO_ORDER_KEY] = draft
    return draft


def _set_state(context: ContextTypes.DEFAULT_TYPE, state: str) -> None:
    context.user_data[AUTO_STATE_KEY] = state


def _state(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get(AUTO_STATE_KEY, "")


def _kb(rows: List[List[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


def kb_main_menu() -> InlineKeyboardMarkup:
    return _kb(
        [
            [InlineKeyboardButton("✅ Оформить заказ", callback_data="AUTO:ORDER")],
            [InlineKeyboardButton("📚 Больше информации", callback_data="AUTO:INFO")],
            [InlineKeyboardButton("🧩 Помощь специалиста", callback_data="AUTO:SPECIALIST")],
            [InlineKeyboardButton("☎️ Поговорить с менеджером", callback_data="AUTO:MANAGER")],
            [InlineKeyboardButton("❌ Неинтересно", callback_data="AUTO:DECLINE")],
        ]
    )


def kb_styles() -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for idx, style_id in enumerate(STYLE_IDS, start=1):
        row.append(InlineKeyboardButton(f"Стиль {style_id}", callback_data=f"AUTO:STYLE:{style_id}"))
        if idx % 3 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("0 — помощь специалиста", callback_data="AUTO:SPECIALIST")])
    rows.append([InlineKeyboardButton("🏠 В меню", callback_data="AUTO:MENU")])
    return _kb(rows)


def kb_materials() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(title, callback_data=f"AUTO:MATERIAL:{key}")] for key, title in MATERIALS.items()]
    rows.append([InlineKeyboardButton("0 — помощь специалиста", callback_data="AUTO:SPECIALIST")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="AUTO:BACK:STYLE")])
    rows.append([InlineKeyboardButton("🏠 В меню", callback_data="AUTO:MENU")])
    return _kb(rows)


def kb_colors(material_key: str) -> InlineKeyboardMarkup:
    colors = COLOR_MAP.get(material_key, [])
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for idx, color_id in enumerate(colors, start=1):
        row.append(InlineKeyboardButton(f"Цвет {color_id}", callback_data=f"AUTO:COLOR:{material_key}:{color_id}"))
        if idx % 3 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("0 — помощь специалиста", callback_data="AUTO:SPECIALIST")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="AUTO:BACK:MATERIAL")])
    rows.append([InlineKeyboardButton("🏠 В меню", callback_data="AUTO:MENU")])
    return _kb(rows)


def kb_insert() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(title, callback_data=f"AUTO:INSERT:{key}")] for key, title in INSERT_TYPES.items()]
    rows.append([InlineKeyboardButton("0 — помощь специалиста", callback_data="AUTO:SPECIALIST")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="AUTO:BACK:COLOR")])
    rows.append([InlineKeyboardButton("🏠 В меню", callback_data="AUTO:MENU")])
    return _kb(rows)


def kb_options(selected: List[str]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for key, title in OPTIONS.items():
        prefix = "✅ " if key in selected else "☑️ "
        rows.append([InlineKeyboardButton(f"{prefix}{title}", callback_data=f"AUTO:OPT:{key}")])
    rows.append([InlineKeyboardButton("0 — не нужно", callback_data="AUTO:OPT:ZERO")])
    rows.append([InlineKeyboardButton("0 — помощь специалиста", callback_data="AUTO:SPECIALIST")])
    rows.append([InlineKeyboardButton("Готово", callback_data="AUTO:OPT:DONE")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="AUTO:BACK:INSERT")])
    rows.append([InlineKeyboardButton("🏠 В меню", callback_data="AUTO:MENU")])
    return _kb(rows)


def kb_payments() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(title, callback_data=f"AUTO:PAY:{key}")] for key, title in PAYMENTS.items()]
    rows.append([InlineKeyboardButton("0 — помощь специалиста", callback_data="AUTO:SPECIALIST")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="AUTO:BACK:OPTIONS")])
    rows.append([InlineKeyboardButton("🏠 В меню", callback_data="AUTO:MENU")])
    return _kb(rows)


def kb_info_topics() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(title, callback_data=f"AUTO:INFO:{key}")] for key, title in INFO_TOPICS.items()]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="AUTO:MENU")])
    return _kb(rows)


def kb_decline_reasons() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(title, callback_data=f"AUTO:DECLINE:{key}")] for key, title in DECLINE_REASONS.items()]
    rows.append([InlineKeyboardButton("0 — помощь специалиста", callback_data="AUTO:SPECIALIST")])
    rows.append([InlineKeyboardButton("🏠 В меню", callback_data="AUTO:MENU")])
    return _kb(rows)


def _summary_text(order: OrderDraft) -> str:
    option_titles = [OPTIONS.get(option_id, option_id) for option_id in order.options]
    return (
        "Проверьте выбор:\n\n"
        f"• Стиль: {order.style_id or '—'}\n"
        f"• Материал: {MATERIALS.get(order.material_id or '', '—')}\n"
        f"• Цвет: {order.color_id or '—'}\n"
        f"• Вставка: {INSERT_TYPES.get(order.insert_type_id or '', '—')}\n"
        f"• Опции: {', '.join(option_titles) if option_titles else '—'}\n"
        f"• Оплата/доставка: {PAYMENTS.get(order.payment_id or '', '—')}\n\n"
        "Если всё верно, нажмите \"Подтвердить\"."
    )


def kb_confirm() -> InlineKeyboardMarkup:
    return _kb(
        [
            [InlineKeyboardButton("Подтвердить", callback_data="AUTO:CONFIRM")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="AUTO:BACK:PAY")],
            [InlineKeyboardButton("🏠 В меню", callback_data="AUTO:MENU")],
        ]
    )


async def start_autochehol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _set_state(context, STATE_MENU)
    if update.message:
        await update.message.reply_text(
            "Здравствуйте! Чем могу помочь?",
            reply_markup=kb_main_menu(),
        )


async def handle_autochehol_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.callback_query:
        return False

    data = (update.callback_query.data or "").strip()
    if not data.startswith("AUTO:"):
        return False

    query = update.callback_query

    if data == "AUTO:MENU":
        _set_state(context, STATE_MENU)
        await query.edit_message_text("Выберите действие:", reply_markup=kb_main_menu())
        return True

    if data == "AUTO:ORDER":
        _set_state(context, STATE_ORDER_STYLE)
        _order_from_context(context)
        await query.edit_message_text("Выберите стиль:", reply_markup=kb_styles())
        return True

    if data.startswith("AUTO:STYLE:"):
        order = _order_from_context(context)
        order.style_id = data.split(":", 2)[2]
        _set_state(context, STATE_ORDER_MATERIAL)
        await query.edit_message_text("Выберите материал:", reply_markup=kb_materials())
        return True

    if data.startswith("AUTO:MATERIAL:"):
        order = _order_from_context(context)
        order.material_id = data.split(":", 2)[2]
        _set_state(context, STATE_ORDER_COLOR)
        await query.edit_message_text("Выберите цвет:", reply_markup=kb_colors(order.material_id))
        return True

    if data.startswith("AUTO:COLOR:"):
        parts = data.split(":", 3)
        if len(parts) == 4:
            material_key = parts[2]
            color_id = parts[3]
            order = _order_from_context(context)
            order.material_id = material_key
            order.color_id = color_id
            if material_key == "dakota":
                _set_state(context, STATE_ORDER_INSERT)
                await query.edit_message_text("Выберите центральную часть:", reply_markup=kb_insert())
                return True
            _set_state(context, STATE_ORDER_OPTIONS)
            await query.edit_message_text("Выберите доп. опции:", reply_markup=kb_options(order.options))
            return True

    if data.startswith("AUTO:INSERT:"):
        order = _order_from_context(context)
        order.insert_type_id = data.split(":", 2)[2]
        _set_state(context, STATE_ORDER_OPTIONS)
        await query.edit_message_text("Выберите доп. опции:", reply_markup=kb_options(order.options))
        return True

    if data.startswith("AUTO:OPT:"):
        action = data.split(":", 2)[2]
        order = _order_from_context(context)
        if action == "ZERO":
            order.options = []
            _set_state(context, STATE_ORDER_PAYMENT)
            await query.edit_message_text("Выберите способ оплаты/доставки:", reply_markup=kb_payments())
            return True
        if action == "DONE":
            _set_state(context, STATE_ORDER_PAYMENT)
            await query.edit_message_text("Выберите способ оплаты/доставки:", reply_markup=kb_payments())
            return True
        if action in OPTIONS:
            if action in order.options:
                order.options.remove(action)
            else:
                order.options.append(action)
            await query.edit_message_text("Выберите доп. опции:", reply_markup=kb_options(order.options))
            return True

    if data.startswith("AUTO:PAY:"):
        order = _order_from_context(context)
        order.payment_id = data.split(":", 2)[2]
        _set_state(context, STATE_ORDER_CONFIRM)
        await query.edit_message_text(_summary_text(order), reply_markup=kb_confirm())
        return True

    if data == "AUTO:CONFIRM":
        _set_state(context, STATE_MENU)
        await query.edit_message_text(
            "Спасибо! Менеджер свяжется с вами в рабочее время (9:00–18:00).",
            reply_markup=kb_main_menu(),
        )
        return True

    if data == "AUTO:SPECIALIST":
        _set_state(context, STATE_SPECIALIST_DETAILS)
        await query.edit_message_text(
            "Опишите, какой стиль/цвет/бюджет вас интересуют.",
        )
        return True

    if data == "AUTO:INFO":
        _set_state(context, STATE_INFO_TOPIC)
        await query.edit_message_text("Выберите тему:", reply_markup=kb_info_topics())
        return True

    if data.startswith("AUTO:INFO:"):
        topic_key = data.split(":", 2)[2]
        topic_title = INFO_TOPICS.get(topic_key, "Тема")
        _set_state(context, STATE_MENU)
        await query.edit_message_text(
            f"{topic_title}: здесь будет контент (описание/видео).\n\n"
            "Хотите оформить заказ или задать вопрос специалисту?",
            reply_markup=kb_main_menu(),
        )
        return True

    if data == "AUTO:MANAGER":
        _set_state(context, STATE_MANAGER_TOPIC)
        await query.edit_message_text("Коротко опишите тему вопроса:")
        return True

    if data == "AUTO:DECLINE":
        _set_state(context, STATE_DECLINE_REASON)
        await query.edit_message_text("Подскажите причину:", reply_markup=kb_decline_reasons())
        return True

    if data.startswith("AUTO:DECLINE:"):
        reason_key = data.split(":", 2)[2]
        if reason_key == "expensive":
            _set_state(context, STATE_MENU)
            await query.edit_message_text(
                "Можем предложить скидку 10% и вышивку в подарок. Оформим заказ?",
                reply_markup=kb_main_menu(),
            )
            return True
        if reason_key == "missing":
            _set_state(context, STATE_SPECIALIST_DETAILS)
            await query.edit_message_text("Подберем персонально. Опишите ваши пожелания:")
            return True
        if reason_key == "browsing":
            _set_state(context, STATE_INFO_TOPIC)
            await query.edit_message_text("Могу показать каталог. Выберите тему:", reply_markup=kb_info_topics())
            return True
        if reason_key == "other":
            _set_state(context, STATE_DECLINE_OTHER)
            await query.edit_message_text("Напишите причину, пожалуйста:")
            return True

    if data.startswith("AUTO:BACK:"):
        target = data.split(":", 2)[2]
        if target == "STYLE":
            _set_state(context, STATE_ORDER_STYLE)
            await query.edit_message_text("Выберите стиль:", reply_markup=kb_styles())
            return True
        if target == "MATERIAL":
            _set_state(context, STATE_ORDER_MATERIAL)
            await query.edit_message_text("Выберите материал:", reply_markup=kb_materials())
            return True
        if target == "COLOR":
            _set_state(context, STATE_ORDER_COLOR)
            order = _order_from_context(context)
            await query.edit_message_text("Выберите цвет:", reply_markup=kb_colors(order.material_id or ""))
            return True
        if target == "INSERT":
            _set_state(context, STATE_ORDER_INSERT)
            await query.edit_message_text("Выберите центральную часть:", reply_markup=kb_insert())
            return True
        if target == "OPTIONS":
            _set_state(context, STATE_ORDER_OPTIONS)
            order = _order_from_context(context)
            await query.edit_message_text("Выберите доп. опции:", reply_markup=kb_options(order.options))
            return True
        if target == "PAY":
            _set_state(context, STATE_ORDER_PAYMENT)
            await query.edit_message_text("Выберите способ оплаты/доставки:", reply_markup=kb_payments())
            return True

    return False


async def handle_autochehol_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message:
        return False

    state = _state(context)
    text = (update.message.text or "").strip()

    if state == STATE_SPECIALIST_DETAILS:
        context.user_data["specialist_details"] = text
        _set_state(context, STATE_SPECIALIST_PHONE)
        await update.message.reply_text("Оставьте телефон для связи:")
        return True

    if state == STATE_SPECIALIST_PHONE:
        context.user_data["specialist_phone"] = text
        _set_state(context, STATE_MENU)
        await update.message.reply_text(
            "Спасибо! Передали менеджеру, свяжемся в рабочее время 9:00–18:00.",
            reply_markup=kb_main_menu(),
        )
        return True

    if state == STATE_MANAGER_TOPIC:
        context.user_data["manager_topic"] = text
        _set_state(context, STATE_MANAGER_PHONE)
        await update.message.reply_text("Укажите телефон для связи:")
        return True

    if state == STATE_MANAGER_PHONE:
        context.user_data["manager_phone"] = text
        _set_state(context, STATE_MENU)
        await update.message.reply_text(
            "Менеджер получил запрос и свяжется с вами в рабочее время.",
            reply_markup=kb_main_menu(),
        )
        return True

    if state == STATE_DECLINE_OTHER:
        context.user_data["decline_reason"] = text
        _set_state(context, STATE_MENU)
        await update.message.reply_text(
            "Спасибо за обратную связь. Если понадобимся — напишите в любое время.",
            reply_markup=kb_main_menu(),
        )
        return True

    return False
