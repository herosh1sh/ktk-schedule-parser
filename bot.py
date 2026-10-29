import asyncio
import json
import logging
import os
import re
from collections import defaultdict
from datetime import date, timedelta
from html import escape
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

import parser

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("Не найден BOT_TOKEN в .env")

SCHEDULE_FILE = Path(__file__).resolve().parent / "schedule.json"
STATE_FILE = Path(__file__).resolve().parent / "bot_state.json"   # подписчики и уже отправленные уведомления
CHECK_INTERVAL = 60        # как часто проверять, не появилась ли следующая неделя (сек)
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MAX_TEXT_LEN = 3800        # лимит Telegram — 4096 символов

router = Router()

# ---------- Поиск дат в JSON ----------

RE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")         # 2026-10-30, 2026-10-30T08:00:00
RE_DOT = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})")       # 30.10.2026


def parse_date(value: Any) -> date | None:
    """Пробует понять, что строка — это дата."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        if m := RE_ISO.match(value):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := RE_DOT.match(value):
            return date(int(m[3]), int(m[2]), int(m[1]))
    except ValueError:
        pass
    return None


def find_days(data: Any) -> dict[date, list[Any]]:
    """
    Рекурсивно обходит JSON и собирает {дата: [куски данных за этот день]}.

    Понимает два варианта:
      1) дата — ключ:            {"2026-10-30": [...]}
      2) дата — значение поля:   {"date": "2026-10-30", "lessons": [...]}
    """
    days: dict[date, list[Any]] = defaultdict(list)

    def walk(node: Any) -> bool:
        found = False

        if isinstance(node, dict):
            # Вариант 1: даты в ключах
            for key, value in node.items():
                d = parse_date(key)
                if d:
                    days[d].append(value)
                    found = True
            if found:
                return True

            # Сначала ищем глубже — нужен самый вложенный «день»,
            # а не вся неделя целиком
            for value in node.values():
                if walk(value):
                    found = True
            if found:
                return True

            # Вариант 2: у этого словаря есть поле с датой
            for value in node.values():
                d = parse_date(value)
                if d:
                    days[d].append(node)
                    return True

        elif isinstance(node, list):
            for item in node:
                if walk(item):
                    found = True

        return found

    walk(data)
    return dict(days)


# ---------- Работа с расписанием ----------

def load_schedule() -> Any | None:
    if not SCHEDULE_FILE.exists():
        return None
    try:
        return json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("Не удалось прочитать schedule.json")
        return None


def render(node: Any, depth: int = 0) -> list[str]:
    """Универсальный вывод данных дня (пока без привязки к структуре)."""
    pad = "    " * depth
    lines: list[str] = []

    if isinstance(node, dict):
        for key, value in node.items():
            if value in (None, "", [], {}):
                continue
            if depth == 0 and parse_date(value):   # саму дату повторять не нужно
                continue
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}<b>{escape(str(key))}:</b>")
                lines.extend(render(value, depth + 1))
            else:
                lines.append(f"{pad}<b>{escape(str(key))}:</b> {escape(str(value))}")

    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                lines.extend(render(item, depth))
                lines.append(f"{pad}———")
            else:
                lines.append(f"{pad}• {escape(str(item))}")
        if lines and lines[-1].strip() == "———":
            lines.pop()

    else:
        lines.append(f"{pad}{escape(str(node))}")

    return lines


def find_lessons(node: Any) -> list[dict]:
    """Находит все пары (словари, где есть поле discipline)."""
    if isinstance(node, dict):
        if "discipline" in node:
            return [node]
        return [lesson for value in node.values() for lesson in find_lessons(value)]
    if isinstance(node, list):
        return [lesson for item in node for lesson in find_lessons(item)]
    return []


HALL_KEY = re.compile(r"hall|room|cabinet|auditor", re.IGNORECASE)
NUMBER_KEY = re.compile(r"^number$", re.IGNORECASE)


def deep_find(node: Any, key_re: re.Pattern) -> list[Any]:
    """Ищет значения всех ключей, подходящих под шаблон, на любой глубине."""
    found: list[Any] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key_re.search(str(key)):
                found.append(value)
            else:
                found.extend(deep_find(value, key_re))
    elif isinstance(node, list):
        for item in node:
            found.extend(deep_find(item, key_re))
    return found


def hall_numbers(value: Any) -> list[str]:
    """Достаёт номер(а) кабинета из словаря, списка или простого значения."""
    if isinstance(value, dict):
        numbers: list[str] = []
        for nested in deep_find(value, NUMBER_KEY):
            numbers.extend(hall_numbers(nested))
        return numbers
    if isinstance(value, list):
        return [n for item in value for n in hall_numbers(item)]
    if value in (None, ""):
        return []
    return [str(value).strip()]


def hall_text(lesson: dict) -> str | None:
    """Ищет кабинет внутри пары: lecture_hall -> number (и похожие варианты)."""
    numbers: list[str] = []
    for value in deep_find(lesson, HALL_KEY):
        numbers.extend(hall_numbers(value))
    numbers = list(dict.fromkeys(numbers))           # убрать дубли, сохранив порядок
    return f"Кабинет {', '.join(numbers)}" if numbers else None


def render_lessons(node: Any) -> list[str]:
    """Только значения полей: type, LessonType, discipline + кабинет в одной строке с предметом."""
    lessons = find_lessons(node)
    if not lessons:                      # пар не нашли — старый универсальный вывод
        return render(node)

    lines: list[str] = []
    for lesson in lessons:
        if lesson.get("type"):
            lines.append(f"🏫 {escape(str(lesson['type']))}")
        if lesson.get("LessonType"):
            lines.append(f"🔹 {escape(str(lesson['LessonType']))}")

        subject = f"📚 <b>{escape(str(lesson.get('discipline', '')))}</b>"
        hall = hall_text(lesson)
        if hall:
            subject += f" — 🚪 {escape(hall)}"
        lines.append(subject)
        lines.append("")                 # пустая строка между парами
    return lines


def format_day(schedule: Any, day: date) -> str | None:
    nodes = find_days(schedule).get(day)
    if not nodes:
        return None

    lines = [f"📅 <b>{WEEKDAYS[day.weekday()]}, {day:%d.%m.%Y}</b>", ""]
    for node in nodes:
        lines.extend(render_lessons(node))
        lines.append("")

    # Режем по строкам, чтобы не сломать HTML-теги
    text, total = [], 0
    for line in lines:
        total += len(line) + 1
        if total > MAX_TEXT_LEN:
            text.append("…")
            break
        text.append(line)
    return "\n".join(text).strip()


# ---------- Клавиатуры ----------

def main_kb(subscribed: bool = True) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Сегодня", callback_data="rel:0")
    kb.button(text="Завтра", callback_data="rel:1")
    kb.button(text="📆 Выбрать день", callback_data="pick")
    kb.button(
        text="🔔 Уведомления: вкл" if subscribed else "🔕 Уведомления: выкл",
        callback_data="notify",
    )
    kb.adjust(2, 1, 1)
    return kb.as_markup()


def pick_kb() -> InlineKeyboardMarkup:
    """Все дни текущей недели (Пн–Вс), независимо от того, что есть в файле."""
    kb = InlineKeyboardBuilder()
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    for i in range(7):
        d = monday + timedelta(days=i)
        mark = "• " if d == today else ""
        kb.button(text=f"{mark}{WEEKDAYS[d.weekday()]} {d:%d.%m}", callback_data=f"d:{d.isoformat()}")
    kb.adjust(4, 3)
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="menu"))
    return kb.as_markup()


def day_kb(day: date, available: list[date]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    prev_days = [d for d in available if d < day]
    next_days = [d for d in available if d > day]

    nav = []
    if prev_days:
        nav.append(("⬅️", f"d:{prev_days[-1].isoformat()}"))
    nav.append(("🔄", f"d:{day.isoformat()}"))
    if next_days:
        nav.append(("➡️", f"d:{next_days[0].isoformat()}"))
    for text, data in nav:
        kb.button(text=text, callback_data=data)
    kb.button(text="📆 Все даты", callback_data="pick")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(len(nav), 2)
    return kb.as_markup()


async def safe_edit(message: Message, text: str, kb: InlineKeyboardMarkup):
    try:
        await message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def show_day(call: CallbackQuery, day: date):
    schedule = load_schedule()
    if schedule is None:
        await call.answer("Расписание ещё не загружено, попробуй позже.", show_alert=True)
        return
    days = find_days(schedule)
    text = format_day(schedule, day)
    if text is None:
        await call.answer(f"На {day:%d.%m.%Y} в расписании ничего нет.", show_alert=True)
        return
    await safe_edit(call.message, text, day_kb(day, sorted(days)))
    await call.answer()


# ---------- Хэндлеры ----------

@router.message(CommandStart())
async def cmd_start(message: Message):
    set_subscription(message.chat.id, True)          # по умолчанию подписываем на уведомления
    await message.answer(
        "Привет! Какое расписание показать?\n"
        "🔔 Я сообщу, когда появится расписание на следующую неделю.",
        reply_markup=main_kb(True),
    )


@router.message(Command("dates"))
async def cmd_dates(message: Message):
    """Отладка: какие даты бот нашёл в schedule.json."""
    schedule = load_schedule()
    if schedule is None:
        await message.answer("schedule.json пока нет.")
        return
    days = sorted(find_days(schedule))
    if not days:
        await message.answer("Не нашёл ни одной даты. Пришли /raw, подстроим разбор.")
        return
    await message.answer(
        f"Найдено дат: {len(days)}\n"
        f"с {days[0]:%d.%m.%Y} по {days[-1]:%d.%m.%Y}\n\n"
        + ", ".join(f"{d:%d.%m}" for d in days)
    )


@router.message(Command("lesson"))
async def cmd_lesson(message: Message):
    """Отладка: сырой JSON первой найденной пары (чтобы проверить кабинет и поля)."""
    schedule = load_schedule()
    lessons = find_lessons(schedule) if schedule else []
    if not lessons:
        await message.answer("Не нашёл ни одной пары (поле discipline).")
        return
    raw = json.dumps(lessons[0], ensure_ascii=False, indent=2)[:3500]
    await message.answer(f"<pre>{escape(raw)}</pre>\nКабинет: {escape(str(hall_text(lessons[0])))}")


@router.message(Command("raw"))
async def cmd_raw(message: Message):
    """Отладка: начало schedule.json."""
    schedule = load_schedule()
    if schedule is None:
        await message.answer("schedule.json пока нет.")
        return
    raw = json.dumps(schedule, ensure_ascii=False, indent=2)[:3500]
    await message.answer(f"<pre>{escape(raw)}</pre>")


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery):
    await safe_edit(call.message, "Какое расписание показать?", main_kb(is_subscribed(call.message.chat.id)))
    await call.answer()


@router.callback_query(F.data == "notify")
async def cb_notify(call: CallbackQuery):
    chat_id = call.message.chat.id
    now = not is_subscribed(chat_id)
    set_subscription(chat_id, now)
    await safe_edit(call.message, "Какое расписание показать?", main_kb(now))
    await call.answer("Уведомления включены" if now else "Уведомления выключены")


@router.callback_query(F.data == "pick")
async def cb_pick(call: CallbackQuery):
    await safe_edit(call.message, "Выбери день этой недели:", pick_kb())
    await call.answer()


@router.callback_query(F.data.startswith("rel:"))
async def cb_relative(call: CallbackQuery):
    offset = int(call.data.split(":")[1])
    await show_day(call, date.today() + timedelta(days=offset))


@router.callback_query(F.data.startswith("d:"))
async def cb_date(call: CallbackQuery):
    await show_day(call, date.fromisoformat(call.data.split(":", 1)[1]))


# ---------- Команды и inline-режим (@бот в любом чате) ----------

async def day_text(day: date) -> str:
    schedule = load_schedule()
    text = format_day(schedule, day) if schedule is not None else None
    return text or f"📅 На {day:%d.%m.%Y} в расписании ничего нет."


@router.message(Command("today"))
async def cmd_today(message: Message):
    await message.answer(await day_text(date.today()))


@router.message(Command("tomorrow"))
async def cmd_tomorrow(message: Message):
    await message.answer(await day_text(date.today() + timedelta(days=1)))


@router.inline_query()
async def inline_menu(query: InlineQuery):
    """Срабатывает, когда в любом чате набирают @имя_бота."""
    options = [
        ("today", "Расписание на сегодня", 0),
        ("tomorrow", "Расписание на завтра", 1),
    ]

    # Если после @бота что-то набрали («сег», «зав») — фильтруем варианты
    typed = query.query.strip().lower()
    filtered = [o for o in options if typed in o[1].lower()] if typed else options
    options = filtered or options

    results = []
    for result_id, title, offset in options:
        day = date.today() + timedelta(days=offset)
        results.append(
            InlineQueryResultArticle(
                id=result_id,
                title=title,
                description=f"{WEEKDAYS[day.weekday()]}, {day:%d.%m.%Y}",
                input_message_content=InputTextMessageContent(
                    message_text=await day_text(day),
                    parse_mode=ParseMode.HTML,
                ),
            )
        )
    await query.answer(results, cache_time=30, is_personal=False)


# ---------- Подписчики и уведомление о следующей неделе ----------

def load_state() -> dict:
    default = {"subscribers": [], "notified_weeks": None}
    if not STATE_FILE.exists():
        return default
    try:
        return {**default, **json.loads(STATE_FILE.read_text(encoding="utf-8"))}
    except Exception:
        logging.exception("Не удалось прочитать bot_state.json")
        return default


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def is_subscribed(chat_id: int) -> bool:
    return chat_id in load_state()["subscribers"]


def set_subscription(chat_id: int, on: bool) -> None:
    state = load_state()
    subs = set(state["subscribers"])
    subs.add(chat_id) if on else subs.discard(chat_id)
    state["subscribers"] = sorted(subs)
    save_state(state)


async def check_next_week(bot: Bot) -> None:
    schedule = load_schedule()
    if schedule is None:
        return

    today = date.today()
    next_monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
    next_sunday = next_monday + timedelta(days=6)
    week_days = sorted(d for d in find_days(schedule) if next_monday <= d <= next_sunday)
    week_key = next_monday.isoformat()

    state = load_state()
    notified = state["notified_weeks"]

    # Первый запуск: просто запоминаем, что уже есть, чтобы не слать лишнего
    if notified is None:
        state["notified_weeks"] = [week_key] if week_days else []
        save_state(state)
        return

    if not week_days or week_key in notified:
        return

    text = (
        "🔔 <b>Появилось расписание на следующую неделю!</b>\n"
        f"📆 {next_monday:%d.%m} – {next_sunday:%d.%m}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="👀 Посмотреть", callback_data=f"d:{week_days[0].isoformat()}")
    markup = kb.as_markup()

    for chat_id in list(state["subscribers"]):
        try:
            await bot.send_message(chat_id, text, reply_markup=markup)
        except TelegramForbiddenError:               # пользователь заблокировал бота
            set_subscription(chat_id, False)
        except Exception:
            logging.exception("Не удалось отправить уведомление %s", chat_id)
        await asyncio.sleep(0.05)                    # не упираемся в лимиты Telegram

    state = load_state()                             # подписки могли измениться
    state["notified_weeks"] = (notified + [week_key])[-10:]
    save_state(state)
    logging.info("Уведомление о неделе %s отправлено", week_key)


async def watch_next_week(bot: Bot) -> None:
    while True:
        try:
            await check_next_week(bot)
        except Exception:
            logging.exception("Ошибка проверки следующей недели")
        await asyncio.sleep(CHECK_INTERVAL)


# ---------- Запуск ----------

async def main():
    logging.basicConfig(level=logging.INFO)

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    # Парсер работает в фоне, пока работает бот
    parser_task = asyncio.create_task(parser.main())
    parser_task.add_done_callback(
        lambda t: logging.error("Парсер остановился: %r", t.exception())
        if not t.cancelled() and t.exception()
        else None
    )

    await bot.set_my_commands([
        BotCommand(command="start", description="Меню расписания"),
        BotCommand(command="today", description="Расписание на сегодня"),
        BotCommand(command="tomorrow", description="Расписание на завтра"),
    ])

    watcher_task = asyncio.create_task(watch_next_week(bot))

    try:
        await dp.start_polling(bot)
    finally:
        parser_task.cancel()
        watcher_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())