import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from config_reader import get_settings
from database.session import close_db, init_db
from handlers.user_router import router as user_router
from middlewares.db import DbSessionMiddleware
from services.rich_messages import ORIGINAL_BOT_USERNAME, configure_bot_identity
from services.scheduler import scheduler_loop


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    settings = get_settings()
    await init_db()

    bot = Bot(
        settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    bot_username = (await bot.get_me()).username
    configure_bot_identity(bot_username)
    if (bot_username or "").casefold() != ORIGINAL_BOT_USERNAME:
        await bot.set_my_description(description="Оригинальный бот: @SurokHabitsBot")
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Открыть главное меню"),
            BotCommand(command="new", description="Создать привычку"),
            BotCommand(command="mem", description="Создать напоминание"),
            BotCommand(command="today", description="План на сегодня"),
            BotCommand(command="habits", description="Мои привычки"),
            BotCommand(command="stats", description="Статистика"),
            BotCommand(command="timezone", description="Изменить часовой пояс"),
        ]
    )

    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(DbSessionMiddleware())
    dp.include_router(user_router)

    scheduler_task = asyncio.create_task(scheduler_loop(bot), name="habit-scheduler")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler_task.cancel()
        await asyncio.gather(scheduler_task, return_exceptions=True)
        await bot.session.close()
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
