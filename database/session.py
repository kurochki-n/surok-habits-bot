from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config_reader import get_settings
from database.base import Base

settings = get_settings()
engine = create_async_engine(settings.database_url, echo=False)
SessionFactory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _migrate_sqlite_schema(connection) -> None:
    if engine.url.get_backend_name() != "sqlite":
        return

    habit_columns = {
        row["name"]
        for row in (
            await connection.execute(text("PRAGMA table_info(habits)"))
        ).mappings()
    }
    if "repetition_labels" not in habit_columns:
        await connection.execute(
            text("ALTER TABLE habits ADD COLUMN repetition_labels JSON")
        )
    if "reminder_times" not in habit_columns:
        await connection.execute(
            text("ALTER TABLE habits ADD COLUMN reminder_times JSON")
        )

    repetition_columns = {
        row["name"]
        for row in (
            await connection.execute(text("PRAGMA table_info(habit_repetitions)"))
        ).mappings()
    }
    if "label" not in repetition_columns:
        await connection.execute(
            text("ALTER TABLE habit_repetitions ADD COLUMN label VARCHAR(80)")
        )


async def init_db() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await _migrate_sqlite_schema(connection)


async def close_db() -> None:
    await engine.dispose()
