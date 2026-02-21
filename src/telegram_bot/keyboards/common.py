from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def back_kb(back_to: str) -> InlineKeyboardMarkup:
    """
    Универсальная кнопка "Назад".
    back_to: строка для callback_data, например "BACK:MENU" или "BACK:SPORTS"
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=back_to)
    return kb.as_markup()
