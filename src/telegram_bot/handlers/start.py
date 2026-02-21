from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from aiogram.fsm.context import FSMContext

from ..states import Flow
from ..keyboards.sports import sports_kb

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Flow.sport)

    text = (
        "Я помогаю разобраться в линии букмекеров.\n"
        "Это аналитика, не ставки.\n\n"
        "Выбери спорт:"
    )

    await message.answer(text, reply_markup=sports_kb())
