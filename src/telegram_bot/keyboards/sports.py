from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def sports_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()

    kb.button(text="🏒 Хоккей", callback_data="SPORT:hockey")

    # Заглушки под расширение (по ТЗ)
    kb.button(text="⚽ Футбол (скоро)", callback_data="SPORT:football_soon")
    kb.button(text="🏀 Баскетбол (скоро)", callback_data="SPORT:basket_soon")

    kb.adjust(1)
    return kb.as_markup()
