from datetime import date
from html import escape

from aiogram import Bot
from aiogram.methods import EditMessageText, SendRichMessage
from aiogram.types import InputRichMessage

from database.models import Habit, HabitDay
from utils.dates import mask_to_text

ORIGINAL_BOT_USERNAME = "surokhabitsbot"
_show_original_bot_notice = False


def configure_bot_identity(bot_username: str | None) -> None:
    global _show_original_bot_notice
    _show_original_bot_notice = (bot_username or "").casefold() != ORIGINAL_BOT_USERNAME


def rich_message(html: str) -> InputRichMessage:
    notice = "<footer>Оригинальный бот: @SurokHabitsBot</footer>"
    return InputRichMessage(html=f"{html}{notice if _show_original_bot_notice else ''}")


def _repetition_labels(habit: Habit) -> list[str]:
    return habit.repetition_labels or [habit.repetition_description] * habit.repetitions


def _button_rows(day: HabitDay) -> str:
    reps = sorted(day.repetitions, key=lambda r: r.position)
    rows = []
    for offset in range(0, len(reps), 4):
        buttons = []
        for rep in reps[offset : offset + 4]:
            text = escape(rep.label or day.habit.repetition_description)
            if rep.is_done:
                buttons.append(
                    f'<tg-button type="disabled" style="success">✓ {text}</tg-button>'
                )
            else:
                buttons.append(
                    f'<tg-button type="callback_data" style="primary" data="rep:{rep.id}">{text}</tg-button>'
                )
        rows.append(f'<tg-button-row align="left">{"".join(buttons)}</tg-button-row>')
    return "".join(rows)


def build_daily_dashboard(
    days: list[HabitDay], streak: int | None = None
) -> InputRichMessage:
    if not days:
        return rich_message(
            "<h3>Привычки на сегодня</h3>"
            "<p>На сегодня ничего не запланировано. Отдых тоже часть системы.</p>"
        )

    total = sum(day.repetitions_total for day in days)
    done = sum(day.repetitions_done for day in days)
    percent = round(done / total * 100) if total else 0
    streak_text = f" · 🔥 <b>{streak}</b>" if streak else ""
    sections = []
    for day in days:
        complete = day.repetitions_done >= day.repetitions_total
        status = "✅" if complete else f"{day.repetitions_done}/{day.repetitions_total}"
        sections.append(
            f"<h4>{escape(day.habit.name)} · {status}</h4>{_button_rows(day)}"
        )

    html = (
        "<h3>Привычки на сегодня</h3>"
        f"<p>Прогресс <b>{done}/{total}</b> · {percent}%{streak_text}</p>"
        f"{''.join(sections)}"
        "<footer>Нажимай на выполненные повторения — результат сохранится автоматически.</footer>"
    )
    return rich_message(html)


def build_habits_message(habits: list[Habit]) -> InputRichMessage:
    if not habits:
        return rich_message(
            "<h3>Мои привычки</h3><p>Активных привычек пока нет.</p>"
            '<tg-button-row><tg-button type="callback_data" style="primary" data="habit:new">Добавить привычку</tg-button></tg-button-row>'
        )
    blocks = ["<h3>Мои привычки</h3>"]
    for habit in habits:
        blocks.append(
            f"<p><b>{escape(habit.name)}</b><br>"
            f"{' · '.join(escape(label) for label in _repetition_labels(habit))}<br>"
            f"{escape(mask_to_text(habit.weekdays_mask))} · {', '.join(habit.reminder_times or [habit.reminder_time.strftime('%H:%M')])}</p>"
            f'<tg-button-row align="left"><tg-button type="callback_data" style="danger" data="habit:delete:{habit.id}">Удалить</tg-button></tg-button-row>'
        )
    blocks.append(
        '<tg-button-row><tg-button type="callback_data" style="primary" data="habit:new">+ Добавить привычку</tg-button></tg-button-row>'
    )
    return rich_message("".join(blocks))


def build_delete_confirmation(habit: Habit) -> InputRichMessage:
    return rich_message(
        "<h3>Удалить привычку?</h3>"
        f"<p><b>{escape(habit.name)}</b><br>{' · '.join(escape(label) for label in _repetition_labels(habit))}</p>"
        "<p>История выполнения и статистика за прошлые дни сохранятся.</p>"
        f'<tg-button-row><tg-button type="callback_data" style="danger" data="habit:confirm_delete:{habit.id}">Да, удалить</tg-button>'
        '<tg-button type="callback_data" data="habit:cancel_delete">Отмена</tg-button></tg-button-row>'
    )


def build_stats_message(
    data: dict, calendar: list[dict], current_day: date
) -> InputRichMessage:
    icons = {
        "done": "✅",
        "partial": "◐",
        "missed": "✕",
        "pending": "·",
        "empty": " ",
        "future": " ",
    }
    weekday_headers = "".join(
        f"<th>{x}</th>" for x in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    )

    # Календарь уже выровнен по полным неделям текущего месяца.
    rows = []
    for offset in range(0, len(calendar), 7):
        week = calendar[offset : offset + 7]
        cells = "".join(
            "<td></td>"
            if item["date"] > current_day and item["date"].month != current_day.month
            else f'<td align="center"><b>{item["date"].day}</b><br>{icons[item["status"]]}</td>'
            for item in week
        )
        rows.append(f"<tr>{cells}</tr>")

    month_names = (
        "январь",
        "февраль",
        "март",
        "апрель",
        "май",
        "июнь",
        "июль",
        "август",
        "сентябрь",
        "октябрь",
        "ноябрь",
        "декабрь",
    )
    month_title = month_names[current_day.month - 1].capitalize()
    completion = data["completion"]
    return rich_message(
        "<h3>Статистика</h3>"
        "<table compact bordered>"
        f'<tr><td>🔥 Текущая серия</td><td align="right"><b>{data["streak"]} дн.</b></td></tr>'
        f'<tr><td>🏆 Рекорд серии</td><td align="right"><b>{data["best_streak"]} дн.</b></td></tr>'
        f'<tr><td>✅ Повторения</td><td align="right"><b>{data["reps_done"]}/{data["reps_total"]}</b></td></tr>'
        f'<tr><td>📈 Выполнение</td><td align="right"><b>{completion}%</b></td></tr>'
        "</table>"
        f"<h4>{month_title}</h4>"
        f"<table compact><tr>{weekday_headers}</tr>{''.join(rows)}</table>"
        "<footer>✅ выполнено · ◐ частично · ✕ пропущено · · сегодня в процессе</footer>"
    )


def build_motivation_message(
    title: str, body: str, streak: int = 0
) -> InputRichMessage:
    streak_line = f"<p>🔥 Активная серия: <b>{streak} дн.</b></p>" if streak else ""
    return rich_message(
        f"<h3>{escape(title)}</h3><p>{escape(body)}</p>{streak_line}"
        '<tg-button-row><tg-button type="callback_data" style="primary" data="open:stats">Открыть статистику</tg-button></tg-button-row>'
    )


async def send_rich(bot: Bot, chat_id: int, rich_message: InputRichMessage):
    return await bot(SendRichMessage(chat_id=chat_id, rich_message=rich_message))


async def send_dashboard(
    bot: Bot, chat_id: int, days: list[HabitDay], streak: int | None = None
):
    return await send_rich(bot, chat_id, build_daily_dashboard(days, streak))


async def edit_dashboard(
    bot: Bot,
    chat_id: int,
    message_id: int,
    days: list[HabitDay],
    streak: int | None = None,
):
    return await bot(
        EditMessageText(
            chat_id=chat_id,
            message_id=message_id,
            rich_message=build_daily_dashboard(days, streak),
        )
    )


def build_weekdays_message(selected_mask: int = 0) -> InputRichMessage:
    names = [
        ("Пн", 0),
        ("Вт", 1),
        ("Ср", 2),
        ("Чт", 3),
        ("Пт", 4),
        ("Сб", 5),
        ("Вс", 6),
    ]
    buttons = []
    for label, index in names:
        selected = bool(selected_mask & (1 << index))
        style = "success" if selected else "primary"
        text = f"✓ {label}" if selected else label
        buttons.append(
            f'<tg-button type="callback_data" style="{style}" data="weekday:{index}">{text}</tg-button>'
        )
    rows = [
        f'<tg-button-row align="left">{"".join(buttons[:4])}</tg-button-row>',
        f'<tg-button-row align="left">{"".join(buttons[4:])}</tg-button-row>',
        '<tg-button-row><tg-button type="callback_data" data="weekday:all">Каждый день</tg-button>'
        '<tg-button type="callback_data" style="success" data="weekday:done">Готово</tg-button></tg-button-row>',
        '<tg-button-row><tg-button type="callback_data" style="danger" data="creation:cancel">Отмена</tg-button></tg-button-row>',
    ]
    return rich_message(
        "<h3>Дни привычки</h3><p>Выбери дни выполнения.</p>" + "".join(rows)
    )


def simple_rich(title: str, body_html: str, buttons_html: str = "") -> InputRichMessage:
    return rich_message(f"<h3>{escape(title)}</h3>{body_html}{buttons_html}")
