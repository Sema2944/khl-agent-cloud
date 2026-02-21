from aiogram.fsm.state import State, StatesGroup


class Flow(StatesGroup):
    sport = State()
    league = State()
    match = State()
    market = State()
