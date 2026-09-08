from datetime import date

WEEKDAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def mask_to_text(mask: int) -> str:
    if mask == 127:
        return "каждый день"
    return ", ".join(name for i, name in enumerate(WEEKDAY_NAMES) if mask & (1 << i))


def days_between(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current = date.fromordinal(current.toordinal() + 1)
