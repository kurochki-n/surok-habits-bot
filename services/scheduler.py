import asyncio
import logging
from datetime import datetime, time, timezone
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot
from sqlalchemy import select

from config_reader import get_settings
from database.models import Habit, Reminder, User
from database.repositories import (
    ensure_habit_day,
    get_or_create_daily_message,
    get_user_day,
    log_notification,
    notification_was_sent,
)
from database.session import SessionFactory
from services.habits import calculate_streak
from services.rich_messages import send_dashboard, send_rich, simple_rich

logger = logging.getLogger(__name__)
MOSCOW = ZoneInfo("Europe/Moscow")


def parse_reminder_times(values: list[str]) -> list[time]:
    parsed = []
    for value in values:
        try:
            parsed.append(time.fromisoformat(value))
        except (TypeError, ValueError):
            continue
    return parsed


def reminder_times(habit: Habit) -> list[time]:
    return parse_reminder_times(
        habit.reminder_times or [habit.reminder_time.strftime("%H:%M")]
    ) or [habit.reminder_time]


def is_current_minute(now: datetime, scheduled: time) -> bool:
    # Do not send a backlog of missed alerts after restarting the bot.
    return (now.hour, now.minute) == (scheduled.hour, scheduled.minute)


async def scheduler_loop(bot: Bot) -> None:
    settings = get_settings()
    while True:
        try:
            await tick(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduler tick failed")
        await asyncio.sleep(settings.scheduler_interval_seconds)


async def tick(bot: Bot) -> None:
    now = datetime.now(timezone.utc)
    morning_due = is_current_minute(now.astimezone(MOSCOW), time(8, 0))
    async with SessionFactory() as session:
        users = list(await session.scalars(select(User)))
        for user in users:
            try:
                zone = ZoneInfo(user.timezone)
            except ZoneInfoNotFoundError:
                zone = ZoneInfo(get_settings().default_timezone)
            local_now = now.astimezone(zone)
            today = local_now.date()

            reminders = list(
                await session.scalars(
                    select(Reminder).where(
                        Reminder.user_id == user.id, Reminder.is_active.is_(True)
                    )
                )
            )
            for reminder in reminders:
                if not reminder.is_scheduled_for(today):
                    continue
                for reminder_time in parse_reminder_times(reminder.reminder_times):
                    kind = f"mem:{reminder.id}:{reminder_time.strftime('%H%M')}"
                    if is_current_minute(
                        local_now, reminder_time
                    ) and not await notification_was_sent(
                        session, user.id, today, kind
                    ):
                        await send_rich(
                            bot,
                            user.id,
                            simple_rich(
                                "Напоминание", f"<p>{escape(reminder.text)}</p>"
                            ),
                        )
                        await log_notification(session, user.id, today, kind)

            habits = list(
                await session.scalars(
                    select(Habit)
                    .where(Habit.user_id == user.id, Habit.is_active.is_(True))
                    .order_by(Habit.id)
                )
            )
            scheduled = [habit for habit in habits if habit.is_scheduled_for(today)]
            if not scheduled:
                continue

            for habit in scheduled:
                await ensure_habit_day(session, habit, today)
            days = await get_user_day(session, user.id, today)
            days_by_habit = {day.habit_id: day for day in days}

            # This log is independent of /today: opening the plan manually must
            # not suppress the daily 08:00 Moscow message.
            if morning_due and not await notification_was_sent(
                session, user.id, today, "morning"
            ):
                streak = await calculate_streak(session, user.id, today)
                message = await send_dashboard(bot, user.id, days, streak)
                message_row = await get_or_create_daily_message(session, user.id, today)
                message_row.telegram_message_id = message.message_id
                message_row.sent_at = now
                await log_notification(session, user.id, today, "morning")

            due_kinds = []
            due_ids = set()
            for habit in scheduled:
                for reminder in reminder_times(habit):
                    if not is_current_minute(local_now, reminder):
                        continue
                    kind = f"reminder:{habit.id}:{reminder.strftime('%H%M')}"
                    if kind in due_kinds or await notification_was_sent(
                        session, user.id, today, kind
                    ):
                        continue
                    due_kinds.append(kind)
                    if not days_by_habit[habit.id].is_completed:
                        due_ids.add(habit.id)

            if due_ids:
                # A reminder coinciding with the morning plan needs no second
                # message: the plan already contains all of today's habits.
                if not morning_due:
                    due_days = [day for day in days if day.habit_id in due_ids]
                    streak = await calculate_streak(session, user.id, today)
                    message = await send_dashboard(bot, user.id, due_days, streak)
                    message_row = await get_or_create_daily_message(
                        session, user.id, today
                    )
                    message_row.telegram_message_id = message.message_id
                    message_row.sent_at = now

            for kind in due_kinds:
                await log_notification(session, user.id, today, kind)
