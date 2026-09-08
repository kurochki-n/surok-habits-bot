from calendar import monthrange
from datetime import date, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Habit, HabitDay
from database.repositories import ensure_habit_day, get_active_habits
from utils.dates import days_between


async def materialize_user_days(
    session: AsyncSession, user_id: int, through: date
) -> None:
    habits = await get_active_habits(session, user_id)
    for habit in habits:
        start = habit.created_at.date() if habit.created_at else through
        for day in days_between(start, through):
            if habit.is_scheduled_for(day):
                await ensure_habit_day(session, habit, day)


async def _daily_statuses(session: AsyncSession, user_id: int, through: date):
    await materialize_user_days(session, user_id, through)
    result = await session.execute(
        select(
            HabitDay.scheduled_date,
            func.count(HabitDay.id),
            func.sum(HabitDay.repetitions_total),
            func.sum(HabitDay.repetitions_done),
            func.sum(
                case(
                    (HabitDay.repetitions_done >= HabitDay.repetitions_total, 1),
                    else_=0,
                )
            ),
        )
        .join(Habit)
        .where(Habit.user_id == user_id, HabitDay.scheduled_date <= through)
        .group_by(HabitDay.scheduled_date)
        .order_by(HabitDay.scheduled_date)
    )
    return result.all()


async def streaks(session: AsyncSession, user_id: int, today: date) -> tuple[int, int]:
    rows = await _daily_statuses(session, user_id, today)
    if not rows:
        return 0, 0

    completed_by_date = {
        d: int(done or 0) == int(total or 0) for d, total, _, _, done in rows
    }
    scheduled_dates = [row[0] for row in rows]

    # Сегодняшний незакрытый день не обрывает текущую серию до конца дня.
    current = 0
    for scheduled_date in reversed(scheduled_dates):
        if scheduled_date == today and not completed_by_date[scheduled_date]:
            continue
        if completed_by_date[scheduled_date]:
            current += 1
        else:
            break

    best = 0
    run = 0
    for scheduled_date in scheduled_dates:
        if completed_by_date[scheduled_date]:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return current, best


async def calculate_streak(session: AsyncSession, user_id: int, today: date) -> int:
    current, _ = await streaks(session, user_id, today)
    return current


async def stats(
    session: AsyncSession, user_id: int, today: date
) -> dict[str, int | float]:
    rows = await _daily_statuses(session, user_id, today)
    habit_days = sum(int(r[1] or 0) for r in rows)
    reps_total = sum(int(r[2] or 0) for r in rows)
    reps_done = sum(int(r[3] or 0) for r in rows)
    completed_habit_days = sum(int(r[4] or 0) for r in rows)
    current, best = await streaks(session, user_id, today)
    completion = round((reps_done / reps_total * 100), 1) if reps_total else 0.0
    return {
        "habit_days": habit_days,
        "reps_total": reps_total,
        "reps_done": reps_done,
        "completed_habit_days": completed_habit_days,
        "completion": completion,
        "streak": current,
        "best_streak": best,
    }


async def calendar_data(session: AsyncSession, user_id: int, today: date) -> list[dict]:
    await materialize_user_days(session, user_id, today)
    first_day = date(today.year, today.month, 1)
    last_day = date(today.year, today.month, monthrange(today.year, today.month)[1])
    # Полные недели текущего месяца: с понедельника перед первым числом до воскресенья после последнего.
    start = first_day - timedelta(days=first_day.weekday())
    end = last_day + timedelta(days=6 - last_day.weekday())
    result = await session.execute(
        select(
            HabitDay.scheduled_date,
            func.count(HabitDay.id),
            func.sum(HabitDay.repetitions_total),
            func.sum(HabitDay.repetitions_done),
            func.sum(
                case(
                    (HabitDay.repetitions_done >= HabitDay.repetitions_total, 1),
                    else_=0,
                )
            ),
        )
        .join(Habit)
        .where(Habit.user_id == user_id, HabitDay.scheduled_date.between(start, today))
        .group_by(HabitDay.scheduled_date)
    )
    by_date = {row[0]: row for row in result.all()}
    output = []
    for i in range((end - start).days + 1):
        day = start + timedelta(days=i)
        row = by_date.get(day)
        if day > today:
            status = "future"
            progress = 0
        elif row is None:
            status = "empty"
            progress = 0
        else:
            _, habit_count, reps_total, reps_done, completed = row
            if int(completed or 0) == int(habit_count or 0):
                status = "done"
            elif int(reps_done or 0) > 0:
                status = "partial"
            else:
                status = "pending" if day == today else "missed"
            progress = round(int(reps_done or 0) / int(reps_total or 1) * 100)
        output.append({"date": day, "status": status, "progress": progress})
    return output
