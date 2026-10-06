import unittest
from datetime import datetime, time, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database.base import Base
from database.models import DailyMessage, Habit, NotificationLog, Reminder, User
from database.repositories import ensure_habit_day
from services import scheduler


class FrozenDatetime(datetime):
    current = datetime(2026, 6, 1, 5, 0, 10, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as session:
            session.add(
                User(
                    id=1,
                    first_name="Test",
                    timezone="Europe/Samara",
                    wake_time=time(7),
                    sleep_time=time(23),
                )
            )
            await session.commit()
        self.dashboard = AsyncMock(return_value=SimpleNamespace(message_id=123))
        self.rich = AsyncMock()
        self.patches = [
            patch.object(scheduler, "SessionFactory", self.sessions),
            patch.object(scheduler, "datetime", FrozenDatetime),
            patch.object(scheduler, "send_dashboard", self.dashboard),
            patch("services.rich_messages.send_rich", self.rich),
        ]
        for item in self.patches:
            item.start()
        self.set_clock(5, 0)

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.engine.dispose()

    def set_clock(self, hour, minute):
        FrozenDatetime.current = datetime(
            2026, 6, 1, hour, minute, 10, tzinfo=timezone.utc
        )

    async def add_habit(self, times=None, mask=127, active=True, completed=False):
        async with self.sessions() as session:
            habit = Habit(
                user_id=1,
                name="Test habit",
                repetitions=1,
                repetition_description="Do it",
                weekdays_mask=mask,
                reminder_time=time(10),
                reminder_times=times or ["10:00"],
                is_active=active,
            )
            session.add(habit)
            await session.commit()
            if completed:
                day = await ensure_habit_day(
                    session, habit, FrozenDatetime.current.date()
                )
                day.repetitions_done = 1
                await session.commit()

    async def test_morning_is_moscow_time_and_sent_once_even_after_manual_today(self):
        await self.add_habit()
        await self.add_habit(times=["17:00"])
        async with self.sessions() as session:
            session.add(
                DailyMessage(
                    user_id=1,
                    scheduled_date=FrozenDatetime.current.date(),
                    telegram_message_id=99,
                )
            )
            await session.commit()
        self.set_clock(4, 59)
        await scheduler.tick(None)
        self.dashboard.assert_not_awaited()
        self.set_clock(5, 0)  # 08:00 Moscow, 09:00 Samara
        await scheduler.tick(None)
        await scheduler.tick(None)
        self.dashboard.assert_awaited_once()
        self.assertEqual(len(self.dashboard.call_args.args[2]), 2)
        self.rich.assert_not_awaited()

    async def test_no_plan_for_empty_or_unscheduled_or_inactive_habits(self):
        await self.add_habit(mask=2)  # Tuesday, while the clock is Monday
        await self.add_habit(active=False)
        await scheduler.tick(None)
        self.dashboard.assert_not_awaited()
        self.rich.assert_not_awaited()

    async def test_individual_habit_times_never_send_notifications(self):
        await self.add_habit(times=["10:00", "12:00"])
        for hour, minute in [(3, 15), (6, 1), (7, 59), (11, 0), (16, 0), (18, 45)]:
            self.set_clock(hour, minute)
            await scheduler.tick(None)
        self.dashboard.assert_not_awaited()
        self.rich.assert_not_awaited()
        for hour in [6, 8]:  # 10:00 and 12:00 Samara
            self.set_clock(hour, 0)
            await scheduler.tick(None)
            await scheduler.tick(None)
        self.dashboard.assert_not_awaited()
        self.rich.assert_not_awaited()

    async def test_completed_habit_has_morning_plan_but_no_scheduled_reminder(self):
        await self.add_habit(completed=True)
        await scheduler.tick(None)
        self.dashboard.assert_awaited_once()
        self.set_clock(6, 0)
        await scheduler.tick(None)
        self.dashboard.assert_awaited_once()
        self.rich.assert_not_awaited()

    async def test_morning_and_habit_reminder_do_not_duplicate(self):
        await self.add_habit(times=["09:00", "09:00"])
        await scheduler.tick(None)
        await scheduler.tick(None)
        self.dashboard.assert_awaited_once()
        async with self.sessions() as session:
            kinds = list(await session.scalars(select(NotificationLog.kind)))
        self.assertCountEqual(kinds, ["morning"])

    async def test_plain_reminders_do_not_send_automatic_notifications(self):
        async with self.sessions() as session:
            session.add_all(
                [
                    Reminder(
                        user_id=1,
                        text="Selected",
                        weekdays_mask=1,
                        reminder_times=["10:00", "12:00"],
                    ),
                    Reminder(
                        user_id=1,
                        text="Wrong day",
                        weekdays_mask=2,
                        reminder_times=["10:00"],
                    ),
                    Reminder(
                        user_id=1,
                        text="Inactive",
                        weekdays_mask=1,
                        reminder_times=["10:00"],
                        is_active=False,
                    ),
                ]
            )
            await session.commit()
        self.set_clock(6, 1)
        await scheduler.tick(None)
        self.rich.assert_not_awaited()
        for hour in [6, 8]:
            self.set_clock(hour, 0)
            await scheduler.tick(None)
            await scheduler.tick(None)
        self.rich.assert_not_awaited()
        self.dashboard.assert_not_awaited()

    async def test_completing_plan_does_not_send_congratulation(self):
        from handlers import user_router

        day = SimpleNamespace(
            scheduled_date=FrozenDatetime.current.date(),
            habit=SimpleNamespace(name="Test"),
        )
        callback = SimpleNamespace(
            data="rep:1",
            from_user=SimpleNamespace(id=1),
            bot=None,
            message=SimpleNamespace(chat=SimpleNamespace(id=1), message_id=123),
            answer=AsyncMock(),
        )
        with (
            patch.object(
                user_router, "mark_repetition_done", AsyncMock(return_value=(day, True))
            ),
            patch.object(user_router, "get_user_day", AsyncMock(return_value=[day])),
            patch.object(user_router, "calculate_streak", AsyncMock(return_value=7)),
            patch.object(user_router, "edit_dashboard", AsyncMock()) as edit,
            patch.object(user_router, "send_rich", AsyncMock()) as send,
        ):
            await user_router.complete_repetition(callback, None)
            edit.assert_awaited_once()
            send.assert_not_awaited()
            callback.answer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
