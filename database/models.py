from datetime import date, datetime, time

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    first_name: Mapped[str] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Samara")

    habits: Mapped[list["Habit"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Habit(Base, TimestampMixin):
    __tablename__ = "habits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(80))
    repetitions: Mapped[int] = mapped_column(Integer)
    repetition_description: Mapped[str] = mapped_column(String(80))
    repetition_labels: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    weekdays_mask: Mapped[int] = mapped_column(Integer, default=127)
    reminder_time: Mapped[time] = mapped_column(Time, default=time(9, 0))
    reminder_times: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    user: Mapped[User] = relationship(back_populates="habits")
    days: Mapped[list["HabitDay"]] = relationship(
        back_populates="habit", cascade="all, delete-orphan"
    )

    def is_scheduled_for(self, day: date) -> bool:
        return bool(self.weekdays_mask & (1 << day.weekday()))


class Reminder(Base, TimestampMixin):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(String(500))
    weekdays_mask: Mapped[int] = mapped_column(Integer, default=127)
    reminder_times: Mapped[list[str]] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    def is_scheduled_for(self, day: date) -> bool:
        return bool(self.weekdays_mask & (1 << day.weekday()))


class HabitDay(Base, TimestampMixin):
    __tablename__ = "habit_days"
    __table_args__ = (
        UniqueConstraint("habit_id", "scheduled_date", name="uq_habit_day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    habit_id: Mapped[int] = mapped_column(
        ForeignKey("habits.id", ondelete="CASCADE"), index=True
    )
    scheduled_date: Mapped[date] = mapped_column(Date, index=True)
    repetitions_total: Mapped[int] = mapped_column(Integer)
    repetitions_done: Mapped[int] = mapped_column(Integer, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    habit: Mapped[Habit] = relationship(back_populates="days")
    repetitions: Mapped[list["HabitRepetition"]] = relationship(
        back_populates="habit_day",
        cascade="all, delete-orphan",
        order_by="HabitRepetition.position",
    )

    @property
    def is_completed(self) -> bool:
        return self.repetitions_done >= self.repetitions_total


class HabitRepetition(Base, TimestampMixin):
    __tablename__ = "habit_repetitions"
    __table_args__ = (
        UniqueConstraint("habit_day_id", "position", name="uq_habit_day_rep"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    habit_day_id: Mapped[int] = mapped_column(
        ForeignKey("habit_days.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    is_done: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    habit_day: Mapped[HabitDay] = relationship(back_populates="repetitions")


class DailyMessage(Base, TimestampMixin):
    __tablename__ = "daily_messages"
    __table_args__ = (
        UniqueConstraint("user_id", "scheduled_date", name="uq_daily_message"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    scheduled_date: Mapped[date] = mapped_column(Date, index=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class NotificationLog(Base, TimestampMixin):
    __tablename__ = "notification_logs"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "scheduled_date", "kind", name="uq_notification_log"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    scheduled_date: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
