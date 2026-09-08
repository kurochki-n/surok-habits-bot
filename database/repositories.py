from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import (
    DailyMessage,
    Habit,
    HabitDay,
    HabitRepetition,
    Reminder,
    User,
)


async def upsert_user(
    session: AsyncSession,
    user_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
    default_timezone: str,
) -> User:
    user = await session.get(User, user_id)
    if user is None:
        user = User(
            id=user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
            timezone=default_timezone,
        )
        session.add(user)
    else:
        user.first_name = first_name
        user.last_name = last_name
        user.username = username
    await session.commit()
    return user


async def create_habit(
    session: AsyncSession,
    user_id: int,
    name: str,
    repetitions: int,
    repetition_description: str,
    repetition_labels: list[str],
    weekdays_mask: int,
    reminder_time,
    reminder_times: list[str],
) -> Habit:
    habit = Habit(
        user_id=user_id,
        name=name,
        repetitions=repetitions,
        repetition_description=repetition_description,
        repetition_labels=repetition_labels,
        weekdays_mask=weekdays_mask,
        reminder_time=reminder_time,
        reminder_times=reminder_times,
    )
    session.add(habit)
    await session.commit()
    await session.refresh(habit)
    return habit


async def create_reminder(
    session: AsyncSession,
    user_id: int,
    text: str,
    weekdays_mask: int,
    reminder_times: list[str],
) -> Reminder:
    reminder = Reminder(
        user_id=user_id,
        text=text,
        weekdays_mask=weekdays_mask,
        reminder_times=reminder_times,
    )
    session.add(reminder)
    await session.commit()
    await session.refresh(reminder)
    return reminder


async def get_active_habits(session: AsyncSession, user_id: int) -> list[Habit]:
    result = await session.scalars(
        select(Habit)
        .where(Habit.user_id == user_id, Habit.is_active.is_(True))
        .order_by(Habit.id)
    )
    return list(result)


async def get_all_active_habits(session: AsyncSession) -> list[Habit]:
    result = await session.scalars(
        select(Habit)
        .options(selectinload(Habit.user))
        .where(Habit.is_active.is_(True))
        .order_by(Habit.id)
    )
    return list(result)


async def ensure_habit_day(
    session: AsyncSession, habit: Habit, day: date
) -> HabitDay | None:
    if not habit.is_active or not habit.is_scheduled_for(day):
        return None

    existing = await session.scalar(
        select(HabitDay)
        .options(selectinload(HabitDay.repetitions), selectinload(HabitDay.habit))
        .where(HabitDay.habit_id == habit.id, HabitDay.scheduled_date == day)
    )
    if existing:
        return existing

    habit_day = HabitDay(
        habit_id=habit.id,
        scheduled_date=day,
        repetitions_total=habit.repetitions,
        repetitions_done=0,
    )
    session.add(habit_day)
    await session.flush()
    session.add_all(
        [
            HabitRepetition(
                habit_day_id=habit_day.id,
                position=i,
                label=(habit.repetition_labels or [habit.repetition_description])[
                    min(i - 1, len(habit.repetition_labels or []) - 1)
                ]
                if habit.repetition_labels
                else habit.repetition_description,
            )
            for i in range(1, habit.repetitions + 1)
        ]
    )
    await session.commit()

    return await session.scalar(
        select(HabitDay)
        .options(selectinload(HabitDay.repetitions), selectinload(HabitDay.habit))
        .where(HabitDay.id == habit_day.id)
    )


async def get_user_day(
    session: AsyncSession, user_id: int, day: date
) -> list[HabitDay]:
    result = await session.scalars(
        select(HabitDay)
        .join(Habit)
        .options(selectinload(HabitDay.repetitions), selectinload(HabitDay.habit))
        .where(
            Habit.user_id == user_id,
            Habit.is_active.is_(True),
            HabitDay.scheduled_date == day,
        )
        .order_by(Habit.id)
    )
    return list(result.unique())


async def mark_repetition_done(
    session: AsyncSession, user_id: int, repetition_id: int
) -> tuple[HabitDay | None, bool]:
    repetition = await session.scalar(
        select(HabitRepetition)
        .options(selectinload(HabitRepetition.habit_day).selectinload(HabitDay.habit))
        .join(HabitDay)
        .join(Habit)
        .where(HabitRepetition.id == repetition_id, Habit.user_id == user_id)
    )
    if repetition is None:
        return None, False
    if repetition.is_done:
        return repetition.habit_day, False

    repetition.is_done = True
    repetition.completed_at = datetime.now(timezone.utc)

    done_count = await session.scalar(
        select(func.count(HabitRepetition.id)).where(
            HabitRepetition.habit_day_id == repetition.habit_day_id,
            HabitRepetition.is_done.is_(True),
        )
    )
    # The current in-memory change isn't necessarily included in SELECT before flush.
    await session.flush()
    done_count = await session.scalar(
        select(func.count(HabitRepetition.id)).where(
            HabitRepetition.habit_day_id == repetition.habit_day_id,
            HabitRepetition.is_done.is_(True),
        )
    )
    repetition.habit_day.repetitions_done = int(done_count or 0)
    just_completed = (
        repetition.habit_day.repetitions_done >= repetition.habit_day.repetitions_total
    )
    if just_completed and repetition.habit_day.completed_at is None:
        repetition.habit_day.completed_at = datetime.now(timezone.utc)
    await session.commit()

    habit_day = await session.scalar(
        select(HabitDay)
        .options(selectinload(HabitDay.repetitions), selectinload(HabitDay.habit))
        .where(HabitDay.id == repetition.habit_day_id)
    )
    return habit_day, just_completed


async def get_or_create_daily_message(
    session: AsyncSession, user_id: int, day: date
) -> DailyMessage:
    item = await session.scalar(
        select(DailyMessage).where(
            DailyMessage.user_id == user_id, DailyMessage.scheduled_date == day
        )
    )
    if item is None:
        item = DailyMessage(user_id=user_id, scheduled_date=day)
        session.add(item)
        await session.commit()
        await session.refresh(item)
    return item


async def deactivate_habit(
    session: AsyncSession, user_id: int, habit_id: int
) -> Habit | None:
    habit = await session.scalar(
        select(Habit).where(
            Habit.id == habit_id, Habit.user_id == user_id, Habit.is_active.is_(True)
        )
    )
    if habit is None:
        return None
    habit.is_active = False
    await session.commit()
    return habit


async def get_habit(session: AsyncSession, user_id: int, habit_id: int) -> Habit | None:
    return await session.scalar(
        select(Habit).where(Habit.id == habit_id, Habit.user_id == user_id)
    )


async def notification_was_sent(
    session: AsyncSession, user_id: int, day: date, kind: str
) -> bool:
    from database.models import NotificationLog

    item = await session.scalar(
        select(NotificationLog.id).where(
            NotificationLog.user_id == user_id,
            NotificationLog.scheduled_date == day,
            NotificationLog.kind == kind,
        )
    )
    return item is not None


async def log_notification(
    session: AsyncSession, user_id: int, day: date, kind: str
) -> None:
    from database.models import NotificationLog

    session.add(
        NotificationLog(
            user_id=user_id,
            scheduled_date=day,
            kind=kind,
            sent_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()


async def remove_incomplete_habit_day(
    session: AsyncSession, user_id: int, habit_id: int, day: date
) -> bool:
    habit_day = await session.scalar(
        select(HabitDay)
        .join(Habit)
        .where(
            Habit.user_id == user_id,
            HabitDay.habit_id == habit_id,
            HabitDay.scheduled_date == day,
            HabitDay.repetitions_done < HabitDay.repetitions_total,
        )
    )
    if habit_day is None:
        return False
    await session.delete(habit_day)
    await session.commit()
    return True
