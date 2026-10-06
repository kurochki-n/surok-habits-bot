import asyncio
import logging
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot
from sqlalchemy import select

from config_reader import get_settings
from database.models import Habit, User
from database.repositories import (
    ensure_habit_day,
    get_or_create_daily_message,
    get_user_day,
    log_notification,
    notification_was_sent,
)
from database.session import SessionFactory
from services.habits import calculate_streak
from services.rich_messages import send_dashboard

logger = logging.getLogger(__name__)
MOSCOW = ZoneInfo("Europe/Moscow")


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
            today = now.astimezone(zone).date()

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

            # Keep generating the day's records for statistics, but never send
            # separate messages at individual habits' reminder times.
            for habit in scheduled:
                await ensure_habit_day(session, habit, today)

            # This log is independent of /today: opening the plan manually must
            # not suppress the daily 08:00 Moscow message.
            if not morning_due or await notification_was_sent(
                session, user.id, today, "morning"
            ):
                continue

            days = await get_user_day(session, user.id, today)
            streak = await calculate_streak(session, user.id, today)
            message = await send_dashboard(bot, user.id, days, streak)
            message_row = await get_or_create_daily_message(session, user.id, today)
            message_row.telegram_message_id = message.message_id
            message_row.sent_at = now
            await log_notification(session, user.id, today, "morning")
