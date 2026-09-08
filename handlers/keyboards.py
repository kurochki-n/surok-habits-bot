from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

WEEKDAYS = [("Пн", 0), ("Вт", 1), ("Ср", 2), ("Чт", 3), ("Пт", 4), ("Сб", 5), ("Вс", 6)]


def weekdays_keyboard(selected_mask: int = 0) -> InlineKeyboardMarkup:
    rows = []
    for label, index in WEEKDAYS:
        selected = bool(selected_mask & (1 << index))
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{'✓ ' if selected else ''}{label}",
                    callback_data=f"weekday:{index}",
                )
            ]
        )
    rows.extend(
        [
            [InlineKeyboardButton(text="Каждый день", callback_data="weekday:all")],
            [InlineKeyboardButton(text="Готово", callback_data="weekday:done")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
