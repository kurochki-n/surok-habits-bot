from datetime import date, datetime, time, timezone
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.methods import EditMessageText, SendRichMessage
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config_reader import get_settings
from database.models import DailyMessage, User
from database.repositories import (
    create_habit,
    create_reminder,
    deactivate_habit,
    deactivate_reminder,
    ensure_habit_day,
    get_active_habits,
    get_active_reminders,
    get_habit,
    get_reminder,
    get_user_day,
    mark_repetition_done,
    remove_incomplete_habit_day,
    reset_statistics,
    upsert_user,
)
from handlers.states import CreateHabit, CreateReminder
from services.habits import (
    calendar_data,
    calculate_streak,
    materialize_user_days,
    stats,
)
from services.rich_messages import (
    build_delete_confirmation,
    build_habits_message,
    build_reminder_delete_confirmation,
    build_reminders_message,
    build_stats_message,
    build_weekdays_message,
    edit_dashboard,
    send_dashboard,
    send_rich,
    simple_rich,
)
from utils.dates import mask_to_text

router = Router(name=__name__)

MSK_TIMEZONES = {
    "-1": "Europe/Kaliningrad",
    "0": "Europe/Moscow",
    "+0": "Europe/Moscow",
    "+1": "Europe/Samara",
    "+2": "Asia/Yekaterinburg",
    "+3": "Asia/Omsk",
    "+4": "Asia/Krasnoyarsk",
    "+5": "Asia/Irkutsk",
    "+6": "Asia/Yakutsk",
    "+7": "Asia/Vladivostok",
    "+8": "Asia/Magadan",
    "+9": "Asia/Kamchatka",
}


async def local_today(session: AsyncSession, user_id: int) -> date:
    user = await session.get(User, user_id)
    timezone_name = user.timezone if user else get_settings().default_timezone
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo(get_settings().default_timezone)
    return datetime.now(timezone.utc).astimezone(zone).date()


async def send_screen(message: Message, rich_message, reply_markup=None):
    return await message.bot(
        SendRichMessage(
            chat_id=message.chat.id,
            rich_message=rich_message,
            reply_markup=reply_markup,
        )
    )


CANCEL_CREATION_BUTTON = (
    '<tg-button-row><tg-button type="callback_data" style="danger" '
    'data="creation:cancel">Отмена</tg-button></tg-button-row>'
)


def wizard_rich(title: str, body_html: str):
    return simple_rich(title, body_html, CANCEL_CREATION_BUTTON)


@router.callback_query(F.data == "creation:cancel")
async def cancel_creation(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Создание отменено")
    if callback.message:
        await callback.bot(
            EditMessageText(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                rich_message=simple_rich(
                    "Создание отменено", "<p>Ничего не сохранено.</p>"
                ),
            )
        )


async def show_today(bot, chat_id: int, user_id: int, session: AsyncSession):
    day = await local_today(session, user_id)
    habits = await get_active_habits(session, user_id)
    for habit in habits:
        await ensure_habit_day(session, habit, day)
    days = await get_user_day(session, user_id, day)
    streak = await calculate_streak(session, user_id, day)
    sent = await send_dashboard(bot, chat_id, days, streak)

    daily_message = await session.scalar(
        select(DailyMessage).where(
            DailyMessage.user_id == user_id, DailyMessage.scheduled_date == day
        )
    )
    if daily_message is None:
        daily_message = DailyMessage(
            user_id=user_id,
            scheduled_date=day,
            telegram_message_id=sent.message_id,
            sent_at=datetime.now(timezone.utc),
        )
        session.add(daily_message)
    else:
        daily_message.telegram_message_id = sent.message_id
        daily_message.sent_at = datetime.now(timezone.utc)
    await session.commit()
    return sent


@router.message(CommandStart())
async def start(message: Message, session: AsyncSession) -> None:
    await upsert_user(
        session,
        message.from_user.id,
        message.from_user.first_name,
        message.from_user.last_name,
        message.from_user.username,
        get_settings().default_timezone,
    )
    if (message.text or "").split(maxsplit=1)[1:] == ["reset_stats"]:
        await send_screen(
            message,
            simple_rich(
                "Сбросить статистику?",
                "<p>Будут удалены история выполнений, серии и календарь. Привычки и напоминания останутся.</p>",
                '<tg-button-row><tg-button type="callback_data" style="danger" data="stats:reset_confirm">Да, сбросить</tg-button>'
                '<tg-button type="callback_data" data="stats:reset_cancel">Отмена</tg-button></tg-button-row>',
            ),
        )
        return
    await send_screen(
        message,
        simple_rich(
            "Трекер привычек\n",
            "<p>Создавай привычки, отмечай каждое выполнение и не теряй серию.</p>"
            '<tg-button-row><tg-button type="callback_data" style="primary" data="habit:new">Создать привычку</tg-button></tg-button-row>',
        ),
        reply_markup=ReplyKeyboardRemove(),
    )


def reminder_weekdays_message(selected_mask: int = 0):
    days = [("Пн", 0), ("Вт", 1), ("Ср", 2), ("Чт", 3), ("Пт", 4), ("Сб", 5), ("Вс", 6)]
    buttons = [
        f'<tg-button type="callback_data" style="{"success" if selected_mask & (1 << index) else "primary"}" data="mem:weekday:{index}">{"✓ " if selected_mask & (1 << index) else ""}{label}</tg-button>'
        for label, index in days
    ]
    rows = [
        f"<tg-button-row>{''.join(buttons[:4])}</tg-button-row>",
        f"<tg-button-row>{''.join(buttons[4:])}</tg-button-row>",
        '<tg-button-row><tg-button type="callback_data" data="mem:weekday:all">Каждый день</tg-button>'
        '<tg-button type="callback_data" style="success" data="mem:weekday:done">Готово</tg-button></tg-button-row>',
    ]
    return wizard_rich("Напоминание · дни", "<p>Выбери дни.</p>" + "".join(rows))


async def begin_new_reminder(bot, chat_id: int, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CreateReminder.text)
    await send_rich(
        bot,
        chat_id,
        wizard_rich("Новое напоминание", "<p>Напиши текст напоминания.</p>"),
    )


@router.message(Command("mem"))
async def reminders_list(message: Message, session: AsyncSession) -> None:
    reminders = await get_active_reminders(session, message.from_user.id)
    await send_screen(message, build_reminders_message(reminders))


@router.callback_query(F.data == "new:reminder")
async def new_reminder_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await begin_new_reminder(callback.bot, callback.from_user.id, state)
    await callback.answer()


@router.message(CreateReminder.text)
async def reminder_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not 1 <= len(text) <= 500:
        await send_screen(
            message,
            simple_rich(
                "Не подходит", "<p>Текст должен быть от 1 до 500 символов.</p>"
            ),
        )
        return
    await state.update_data(text=text, weekdays_mask=0)
    await state.set_state(CreateReminder.weekdays)
    await send_screen(message, reminder_weekdays_message())


@router.callback_query(CreateReminder.weekdays, F.data.startswith("mem:weekday:"))
async def reminder_weekdays(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.rsplit(":", 1)[1]
    data = await state.get_data()
    mask = int(data.get("weekdays_mask", 0))
    if action == "all":
        mask = 127
    elif action == "done":
        if mask == 0:
            await callback.answer("Выберите хотя бы один день", show_alert=True)
            return
        await state.update_data(weekdays_mask=mask)
        await state.set_state(CreateReminder.reminder_count)
        if callback.message:
            await callback.bot(
                EditMessageText(
                    chat_id=callback.message.chat.id,
                    message_id=callback.message.message_id,
                    rich_message=wizard_rich(
                        "Напоминание · количество",
                        "<p>Сколько напоминаний отправлять в день? Введите положительное число.</p>",
                    ),
                )
            )
        await callback.answer()
        return
    else:
        mask ^= 1 << int(action)
    await state.update_data(weekdays_mask=mask)
    if callback.message:
        await callback.bot(
            EditMessageText(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                rich_message=reminder_weekdays_message(mask),
            )
        )
    await callback.answer()


@router.message(CreateReminder.reminder_count)
async def reminder_count(message: Message, state: FSMContext) -> None:
    try:
        count = int(message.text or "")
    except ValueError:
        count = 0
    if count < 1:
        await send_screen(
            message, simple_rich("Не подходит", "<p>Введите положительное число.</p>")
        )
        return
    await state.update_data(reminder_count=count, reminder_times=[])
    await state.set_state(CreateReminder.reminder_time)
    await send_screen(
        message,
        wizard_rich(
            "Напоминание · время",
            f"<p>Введите время <b>1 из {count}</b> в формате <b>09:00</b>.</p>",
        ),
    )


@router.message(CreateReminder.reminder_time)
async def reminder_time(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    try:
        hours, minutes = map(int, (message.text or "").strip().split(":"))
        value = time(hours, minutes).strftime("%H:%M")
    except (ValueError, TypeError):
        await send_screen(
            message,
            simple_rich(
                "Неверное время",
                "<p>Введите время как <b>09:00</b> или <b>18:30</b>.</p>",
            ),
        )
        return
    data = await state.get_data()
    times = [*data.get("reminder_times", []), value]
    count = int(data["reminder_count"])
    if len(times) < count:
        await state.update_data(reminder_times=times)
        number = len(times) + 1
        await send_screen(
            message,
            wizard_rich(
                "Напоминание · время",
                f"<p>Введите время <b>{number} из {count}</b> в формате <b>09:00</b>.</p>",
            ),
        )
        return
    reminder = await create_reminder(
        session, message.from_user.id, data["text"], int(data["weekdays_mask"]), times
    )
    await state.clear()
    await send_screen(
        message,
        simple_rich(
            "Напоминание создано",
            f"<p>{escape(reminder.text)}<br>{mask_to_text(reminder.weekdays_mask)} · {', '.join(reminder.reminder_times)}</p>",
        ),
    )


async def begin_new_habit(chat_message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CreateHabit.name)
    await send_screen(
        chat_message,
        wizard_rich(
            "Новая привычка · 1/5",
            "<p>Как называется привычка?</p><p><i>Например: Отжимания</i></p>",
        ),
    )


@router.message(Command("new"))
async def new_item(message: Message, state: FSMContext) -> None:
    await state.clear()
    await send_screen(
        message,
        simple_rich(
            "Создать",
            "<p>Что ты хочешь создать?</p>",
            '<tg-button-row><tg-button type="callback_data" style="primary" data="new:habit">Привычку</tg-button>'
            '<tg-button type="callback_data" style="primary" data="new:reminder">Напоминание</tg-button></tg-button-row>',
        ),
    )


@router.callback_query(F.data == "new:habit")
async def new_habit_choice(callback: CallbackQuery, state: FSMContext) -> None:
    await begin_new_habit(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "habit:new")
async def new_habit_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CreateHabit.name)
    await send_rich(
        callback.bot,
        callback.from_user.id,
        wizard_rich(
            "Новая привычка · 1/5",
            "<p>Как называется привычка?</p><p><i>Например: Отжимания</i></p>",
        ),
    )
    await callback.answer()


@router.message(CreateHabit.name)
async def habit_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not 1 <= len(name) <= 80:
        await send_screen(
            message,
            simple_rich(
                "Не подходит", "<p>Название должно быть от 1 до 80 символов.</p>"
            ),
        )
        return
    await state.update_data(name=name)
    await state.set_state(CreateHabit.repetitions)
    await send_screen(
        message,
        wizard_rich(
            "Новая привычка · 2/5",
            "<p>Сколько повторений нужно выполнить за день?</p><p><i>Например: 3</i></p>",
        ),
    )


@router.message(CreateHabit.repetitions)
async def habit_repetitions(message: Message, state: FSMContext) -> None:
    try:
        repetitions = int(message.text or "")
    except ValueError:
        repetitions = 0
    if not 1 <= repetitions <= 20:
        await send_screen(
            message, simple_rich("Не подходит", "<p>Введите число от 1 до 20.</p>")
        )
        return
    await state.update_data(repetitions=repetitions, repetition_labels=[])
    await state.set_state(CreateHabit.repetition_label)
    await send_screen(
        message,
        wizard_rich(
            "Новая привычка · повторение 1",
            f"<p>Как назвать повторение <b>1 из {repetitions}</b>?</p>"
            "<p><i>Например: 10 отжиманий</i></p>",
        ),
    )


@router.message(CreateHabit.repetition_label)
async def habit_repetition_label(message: Message, state: FSMContext) -> None:
    label = (message.text or "").strip()
    if not 1 <= len(label) <= 80:
        await send_screen(
            message,
            simple_rich(
                "Не подходит", "<p>Название должно быть от 1 до 80 символов.</p>"
            ),
        )
        return

    data = await state.get_data()
    repetitions = int(data["repetitions"])
    labels = [*data.get("repetition_labels", []), label]
    if len(labels) < repetitions:
        await state.update_data(repetition_labels=labels)
        number = len(labels) + 1
        await send_screen(
            message,
            wizard_rich(
                f"Новая привычка · повторение {number}",
                f"<p>Как назвать повторение <b>{number} из {repetitions}</b>?</p>",
            ),
        )
        return

    await state.update_data(
        repetition_labels=labels,
        repetition_description=labels[0],
        weekdays_mask=0,
    )
    await state.set_state(CreateHabit.weekdays)
    await send_screen(message, build_weekdays_message())


@router.callback_query(CreateHabit.weekdays, F.data.startswith("weekday:"))
async def habit_weekdays(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":", 1)[1]
    data = await state.get_data()
    mask = int(data.get("weekdays_mask", 0))

    if action == "all":
        mask = 127
    elif action == "done":
        if mask == 0:
            await callback.answer("Выберите хотя бы один день", show_alert=True)
            return
        await state.update_data(weekdays_mask=mask)
        await state.set_state(CreateHabit.reminder_count)
        if callback.message:
            await callback.bot(
                EditMessageText(
                    chat_id=callback.message.chat.id,
                    message_id=callback.message.message_id,
                    rich_message=wizard_rich(
                        "Новая привычка · напоминания",
                        f"<p>Дни: <b>{mask_to_text(mask)}</b></p>"
                        "<p>Сколько напоминаний нужно в день? Отправь число от <b>1</b> до <b>5</b>.</p>",
                    ),
                )
            )
        await callback.answer()
        return
    else:
        index = int(action)
        mask ^= 1 << index

    await state.update_data(weekdays_mask=mask)
    if callback.message:
        await callback.bot(
            EditMessageText(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                rich_message=build_weekdays_message(mask),
            )
        )
    await callback.answer()


@router.message(CreateHabit.reminder_count)
async def habit_reminder_count(message: Message, state: FSMContext) -> None:
    try:
        reminder_count = int(message.text or "")
    except ValueError:
        reminder_count = 0
    if not 1 <= reminder_count <= 5:
        await send_screen(
            message,
            simple_rich("Не подходит", "<p>Введите число от 1 до 5.</p>"),
        )
        return

    await state.update_data(reminder_count=reminder_count, reminder_times=[])
    await state.set_state(CreateHabit.reminder_time)
    await send_screen(
        message,
        wizard_rich(
            "Новая привычка · время напоминания",
            f"<p>Введите время напоминания <b>1 из {reminder_count}</b> в формате <b>09:00</b>.</p>",
        ),
    )


@router.message(CreateHabit.reminder_time)
async def habit_reminder_time(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    try:
        hours, minutes = map(int, (message.text or "").strip().split(":"))
        reminder = time(hours, minutes)
    except (ValueError, TypeError):
        await send_screen(
            message,
            simple_rich(
                "Неверное время",
                "<p>Введите время как <b>09:00</b> или <b>18:30</b>.</p>",
            ),
        )
        return

    data = await state.get_data()
    reminder_times = [*data.get("reminder_times", []), reminder.strftime("%H:%M")]
    reminder_count = int(data["reminder_count"])
    if len(reminder_times) < reminder_count:
        await state.update_data(reminder_times=reminder_times)
        number = len(reminder_times) + 1
        await send_screen(
            message,
            wizard_rich(
                "Новая привычка · время напоминания",
                f"<p>Введите время напоминания <b>{number} из {reminder_count}</b> в формате <b>09:00</b>.</p>",
            ),
        )
        return

    habit = await create_habit(
        session=session,
        user_id=message.from_user.id,
        name=data["name"],
        repetitions=data["repetitions"],
        repetition_description=data["repetition_description"],
        repetition_labels=data["repetition_labels"],
        weekdays_mask=data["weekdays_mask"],
        reminder_time=reminder,
        reminder_times=reminder_times,
    )
    await state.clear()
    await send_screen(
        message,
        simple_rich(
            "Привычка создана",
            f"<p><b>{habit.name}</b><br>{' · '.join(habit.repetition_labels or [habit.repetition_description])}<br>"
            f"{mask_to_text(habit.weekdays_mask)} · {', '.join(habit.reminder_times or [habit.reminder_time.strftime('%H:%M')])}</p>",
            '<tg-button-row><tg-button type="callback_data" style="primary" data="habit:new">Добавить ещё</tg-button>'
            '<tg-button type="callback_data" data="open:today">Сегодня</tg-button></tg-button-row>',
        ),
    )


@router.message(Command("habits"))
async def habits_list(message: Message, session: AsyncSession) -> None:
    habits = await get_active_habits(session, message.from_user.id)
    await send_screen(message, build_habits_message(habits))


@router.callback_query(F.data.startswith("habit:delete:"))
async def delete_habit_prompt(callback: CallbackQuery, session: AsyncSession) -> None:
    habit_id = int(callback.data.rsplit(":", 1)[1])
    habit = await get_habit(session, callback.from_user.id, habit_id)
    if habit is None or not habit.is_active:
        await callback.answer("Привычка уже удалена", show_alert=True)
        return
    if callback.message:
        await callback.bot(
            EditMessageText(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                rich_message=build_delete_confirmation(habit),
            )
        )
    await callback.answer()


@router.callback_query(F.data.startswith("habit:confirm_delete:"))
async def delete_habit_confirm(callback: CallbackQuery, session: AsyncSession) -> None:
    habit_id = int(callback.data.rsplit(":", 1)[1])
    day = await local_today(session, callback.from_user.id)
    await materialize_user_days(session, callback.from_user.id, day)
    await remove_incomplete_habit_day(session, callback.from_user.id, habit_id, day)
    habit = await deactivate_habit(session, callback.from_user.id, habit_id)
    if habit is None:
        await callback.answer("Привычка уже удалена", show_alert=True)
        return
    habits = await get_active_habits(session, callback.from_user.id)
    if callback.message:
        await callback.bot(
            EditMessageText(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                rich_message=build_habits_message(habits),
            )
        )
    await callback.answer(f"{habit.name} удалена")


@router.callback_query(F.data == "habit:cancel_delete")
async def delete_habit_cancel(callback: CallbackQuery, session: AsyncSession) -> None:
    habits = await get_active_habits(session, callback.from_user.id)
    if callback.message:
        await callback.bot(
            EditMessageText(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                rich_message=build_habits_message(habits),
            )
        )
    await callback.answer()


@router.callback_query(F.data.startswith("reminder:delete:"))
async def delete_reminder_prompt(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    reminder_id = int(callback.data.rsplit(":", 1)[1])
    reminder = await get_reminder(session, callback.from_user.id, reminder_id)
    if reminder is None or not reminder.is_active:
        await callback.answer("Напоминание уже удалено", show_alert=True)
        return
    if callback.message:
        await callback.bot(
            EditMessageText(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                rich_message=build_reminder_delete_confirmation(reminder),
            )
        )
    await callback.answer()


@router.callback_query(F.data.startswith("reminder:confirm_delete:"))
async def delete_reminder_confirm(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    reminder_id = int(callback.data.rsplit(":", 1)[1])
    reminder = await deactivate_reminder(session, callback.from_user.id, reminder_id)
    if reminder is None:
        await callback.answer("Напоминание уже удалено", show_alert=True)
        return
    reminders = await get_active_reminders(session, callback.from_user.id)
    if callback.message:
        await callback.bot(
            EditMessageText(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                rich_message=build_reminders_message(reminders),
            )
        )
    await callback.answer("Напоминание удалено")


@router.callback_query(F.data == "reminder:cancel_delete")
async def delete_reminder_cancel(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    reminders = await get_active_reminders(session, callback.from_user.id)
    if callback.message:
        await callback.bot(
            EditMessageText(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                rich_message=build_reminders_message(reminders),
            )
        )
    await callback.answer()


@router.message(Command("today"))
async def today(message: Message, session: AsyncSession) -> None:
    await show_today(message.bot, message.chat.id, message.from_user.id, session)


@router.message(Command("sleep"))
async def sleep_schedule(message: Message, session: AsyncSession) -> None:
    """Keep the legacy command without enabling routine notifications."""
    user = await session.get(User, message.from_user.id)
    if user is None:
        await send_screen(
            message,
            simple_rich(
                "Сначала начни", "<p>Отправь /start, затем настрой расписание.</p>"
            ),
        )
        return

    args = (message.text or "").split()[1:]
    if args == ["off"]:
        user.wake_time = None
        user.sleep_time = None
        await session.commit()
        await send_screen(
            message,
            simple_rich(
                "Расписание отключено",
                "<p>Уведомления по режиму дня больше не придут.</p>",
            ),
        )
        return

    if len(args) != 2:
        await send_screen(
            message,
            simple_rich(
                "Режим дня",
                "<p>Уведомления по режиму дня отключены.</p>"
                "<p>Автоматически приходит только общий план привычек в 08:00 по Москве.</p>",
            ),
        )
        return

    try:
        wake_time, sleep_time = (time.fromisoformat(value) for value in args)
    except ValueError:
        await send_screen(
            message,
            simple_rich(
                "Неверное время",
                "<p>Используй формат <b>ЧЧ:ММ</b>, например /sleep 07:00 23:00.</p>",
            ),
        )
        return

    if wake_time == sleep_time:
        await send_screen(
            message,
            simple_rich(
                "Неверное расписание", "<p>Время подъёма и сна не должно совпадать.</p>"
            ),
        )
        return

    user.wake_time = wake_time
    user.sleep_time = sleep_time
    await session.commit()
    await send_screen(
        message,
        simple_rich(
            "Режим дня сохранён",
            f"<p>Подъём: <b>{wake_time:%H:%M}</b> · сон: <b>{sleep_time:%H:%M}</b>.</p>"
            "<p>Уведомления по режиму дня отключены. Это расписание не создаёт новых уведомлений.</p>",
        ),
    )


@router.callback_query(F.data == "open:today")
async def today_callback(callback: CallbackQuery, session: AsyncSession) -> None:
    await show_today(
        callback.bot, callback.from_user.id, callback.from_user.id, session
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rep:"))
async def complete_repetition(callback: CallbackQuery, session: AsyncSession) -> None:
    repetition_id = int(callback.data.split(":", 1)[1])
    habit_day, just_completed = await mark_repetition_done(
        session, callback.from_user.id, repetition_id
    )
    if habit_day is None:
        await callback.answer("Это повторение больше недоступно", show_alert=True)
        return

    day = habit_day.scheduled_date
    days = await get_user_day(session, callback.from_user.id, day)
    streak = await calculate_streak(session, callback.from_user.id, day)

    if callback.message:
        await edit_dashboard(
            callback.bot,
            callback.message.chat.id,
            callback.message.message_id,
            days,
            streak,
        )

    if not just_completed:
        await callback.answer("Засчитано ✓")
        return

    await callback.answer(f"{habit_day.habit.name} выполнена!")


@router.callback_query(F.data == "stats:reset_confirm")
async def reset_statistics_confirm(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    await reset_statistics(session, callback.from_user.id)
    if callback.message:
        await callback.bot(
            EditMessageText(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                rich_message=simple_rich(
                    "Статистика сброшена",
                    "<p>История выполнений, серии и календарь очищены. Привычки и напоминания сохранены.</p>",
                ),
            )
        )
    await callback.answer("Статистика сброшена")


@router.callback_query(F.data == "stats:reset_cancel")
async def reset_statistics_cancel(callback: CallbackQuery) -> None:
    if callback.message:
        await callback.bot(
            EditMessageText(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                rich_message=simple_rich(
                    "Сброс отменён", "<p>Статистика не изменена.</p>"
                ),
            )
        )
    await callback.answer()


@router.callback_query(F.data == "open:stats")
async def statistics_callback(callback: CallbackQuery, session: AsyncSession) -> None:
    day = await local_today(session, callback.from_user.id)
    data = await stats(session, callback.from_user.id, day)
    calendar = await calendar_data(session, callback.from_user.id, day)
    await send_rich(
        callback.bot,
        callback.from_user.id,
        build_stats_message(data, calendar, day),
    )
    await callback.answer()


@router.message(Command("stats"))
async def statistics(message: Message, session: AsyncSession) -> None:
    day = await local_today(session, message.from_user.id)
    data = await stats(session, message.from_user.id, day)
    calendar = await calendar_data(session, message.from_user.id, day)
    await send_screen(message, build_stats_message(data, calendar, day))


@router.message(Command("timezone"))
async def timezone_command(message: Message, session: AsyncSession) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 1:
        user = await session.get(User, message.from_user.id)
        current = user.timezone if user else get_settings().default_timezone
        await send_screen(
            message,
            simple_rich(
                "Часовой пояс",
                f"<p>Текущий: <code>{current}</code></p>"
                "<p>Укажи смещение относительно МСК:<br>"
                "<code>/timezone +1</code> — Самара, <code>/timezone 0</code> — Москва.</p>",
            ),
        )
        return
    offset = parts[1].strip()
    timezone_name = MSK_TIMEZONES.get(offset)
    if timezone_name is None:
        await send_screen(
            message,
            simple_rich(
                "Неизвестное смещение",
                "<p>Укажи смещение от <b>−1</b> до <b>+9</b> относительно МСК, например <code>/timezone +1</code>.</p>",
            ),
        )
        return
    user = await session.get(User, message.from_user.id)
    if user is None:
        await upsert_user(
            session,
            message.from_user.id,
            message.from_user.first_name,
            message.from_user.last_name,
            message.from_user.username,
            timezone_name,
        )
        user = await session.get(User, message.from_user.id)
    user.timezone = timezone_name
    await session.commit()
    await send_screen(
        message,
        simple_rich("Часовой пояс изменён", f"<p><code>{timezone_name}</code></p>"),
    )
