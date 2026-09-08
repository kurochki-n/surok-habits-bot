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
from services.rich_messages import (
    build_motivation_message,
    send_dashboard,
    send_rich,
    simple_rich,
)

logger = logging.getLogger(__name__)


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
    async with SessionFactory() as session:
        users = list(await session.scalars(select(User)))
        for user in users:
            try:
                zone = ZoneInfo(user.timezone)
            except ZoneInfoNotFoundError:
                zone = ZoneInfo(get_settings().default_timezone)
            local_now = datetime.now(timezone.utc).astimezone(zone)
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
                    if (local_now.hour, local_now.minute) >= (
                        reminder_time.hour,
                        reminder_time.minute,
                    ) and not await notification_was_sent(
                        session, user.id, today, kind
                    ):
                        await send_rich(
                            bot,
                            user.id,
                            simple_rich(
                                "Напоминание",
                                f"<p>{escape(reminder.text)}</p>",
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

            due_reminders = []
            for habit in scheduled:
                for reminder in reminder_times(habit):
                    kind = f"reminder:{habit.id}:{reminder.strftime('%H%M')}"
                    if (local_now.hour, local_now.minute) >= (
                        reminder.hour,
                        reminder.minute,
                    ) and not await notification_was_sent(
                        session, user.id, today, kind
                    ):
                        due_reminders.append((habit, reminder, kind))
            if not due_reminders:
                continue

            message_row = await get_or_create_daily_message(session, user.id, today)
            days = await get_user_day(session, user.id, today)
            due_ids = {habit.id for habit, _, _ in due_reminders}
            due_days = [day for day in days if day.habit_id in due_ids]

            if message_row.telegram_message_id is None and due_days:
                streak = await calculate_streak(session, user.id, today)
                message = await send_dashboard(bot, user.id, due_days, streak)
                message_row.telegram_message_id = message.message_id
                message_row.sent_at = datetime.now(timezone.utc)
                await session.commit()
            elif days and not all(day.is_completed for day in days):
                habit_names = ", ".join({habit.name for habit, _, _ in due_reminders})
                await send_rich(
                    bot,
                    user.id,
                    build_motivation_message(
                        "Напоминание",
                        f"Пора уделить время привычкам: {habit_names}.",
                        await calculate_streak(session, user.id, today),
                    ),
                )

            for _, _, kind in due_reminders:
                await log_notification(session, user.id, today, kind)

            if not days or all(day.is_completed for day in days):
                continue

            total = sum(day.repetitions_total for day in days)
            done = sum(day.repetitions_done for day in days)
            remaining = max(total - done, 0)
            percent = round(done / total * 100) if total else 0
            streak = await calculate_streak(session, user.id, today)

            # Дневной мягкий нудж: один раз после 15:00 и только если есть что заканчивать.
            if local_now.hour >= 15 and not await notification_was_sent(
                session, user.id, today, "progress"
            ):
                if done == 0:
                    body = f"Сегодня ещё не было отметок. Начни с одного действия — осталось {remaining}."
                elif percent < 60:
                    body = f"Уже есть прогресс: {done}/{total}. Осталось {remaining} — можно закрыть день без рывка вечером."
                else:
                    body = f"Ты уже сделал {percent}% плана. Осталось всего {remaining} — день почти закрыт."
                await send_rich(
                    bot,
                    user.id,
                    build_motivation_message("Небольшой шаг сейчас", body, streak),
                )
                await log_notification(session, user.id, today, "progress")

            # Вечернее предупреждение о серии: один раз после 20:00.
            if local_now.hour >= 20 and not await notification_was_sent(
                session, user.id, today, "streak_risk"
            ):
                if streak > 0:
                    body = f"До полного дня осталось {remaining}. Закрой их сегодня, чтобы сохранить серию {streak} дн."
                    title = "Серия ещё в твоих руках"
                else:
                    body = f"Осталось {remaining}. Закрой сегодняшний план и начни новую серию с чистого дня."
                    title = "Закрой день"
                await send_rich(
                    bot, user.id, build_motivation_message(title, body, streak)
                )
                await log_notification(session, user.id, today, "streak_risk")
