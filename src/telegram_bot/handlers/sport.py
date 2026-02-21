from aiogram import Router
from aiogram.types import CallbackQuery
from aiogram.fsm.context import FSMContext

from ..states import Flow

router = Router()


@router.callback_query(lambda c: (c.data or "").startswith("SPORT:"))
async def pick_sport(callback: CallbackQuery, state: FSMContext) -> None:
    data = callback.data or ""
    await callback.answer()

    sport = data.split(":", 1)[1]

    # Заглушки
    if sport.endswith("_soon"):
        await callback.message.edit_text(
            "Этот спорт появится позже. Пока доступен 🏒 Хоккей.\n\nВыбери спорт:",
            reply_markup=callback.message.reply_markup,
        )
        return

    # Сохраняем в FSM
    await state.update_data(sport=sport)
    await state.set_state(Flow.league)

    # Следующий экран (лиги) сделаем на следующем шаге.
    await callback.message.edit_text(
        "Ок, выбран спорт: 🏒 Хоккей.\n\nДальше будет выбор лиги (КХЛ/НХЛ)."
    )
