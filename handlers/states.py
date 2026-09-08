from aiogram.fsm.state import State, StatesGroup


class CreateReminder(StatesGroup):
    text = State()
    weekdays = State()
    reminder_count = State()
    reminder_time = State()


class CreateHabit(StatesGroup):
    name = State()
    repetitions = State()
    repetition_label = State()
    weekdays = State()
    reminder_count = State()
    reminder_time = State()
