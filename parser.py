import asyncio
import json
import time
from pathlib import Path

from playwright.async_api import async_playwright


BASE_URL = "https://ktk-45.ru"
API_URL = "https://ktk-45.ru/api/v1/timetable/268"

OUTPUT_FILE = Path(__file__).resolve().parent / "schedule.json"
PROFILE_DIR = Path(__file__).resolve().parent / "browser_profile"

# 30 минут
UPDATE_INTERVAL = 30 * 60


async def get_schedule(page):
    """
    Получает расписание через Playwright.
    """

    # Каждый раз создаём новый timestamp
    url = API_URL + "?time=" + str(int(time.time() * 1000))

    print(f"[INFO] Запрашиваю расписание: {url}")

    # Обновляем страницу
    await page.goto(
        BASE_URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    # Получаем localStorage и sessionStorage
    storage = await page.evaluate("""() => {
        const result = {};

        for (const store of [localStorage, sessionStorage]) {
            for (let i = 0; i < store.length; i++) {
                const key = store.key(i);
                result[key] = store.getItem(key);
            }
        }

        return result;
    }""")

    # Ищем токены
    candidates = []

    def inspect(value, key=""):
        if isinstance(value, dict):

            for k, v in value.items():
                inspect(v, str(k))

        elif isinstance(value, list):

            for item in value:
                inspect(item, key)

        elif isinstance(value, str):

            if any(word in key.lower() for word in (
                "token",
                "auth",
                "access",
                "jwt"
            )):
                candidates.append(value.strip())

            if value.startswith("{") or value.startswith("["):
                try:
                    inspect(json.loads(value), key)
                except json.JSONDecodeError:
                    pass

    inspect(storage)

    # Сначала пробуем cookies,
    # затем найденные токены
    tokens = [None] + list(dict.fromkeys(candidates))

    for token in tokens:

        result = await page.evaluate(
            """async ({url, token}) => {

                const headers = {
                    "Accept": "application/json"
                };

                if (token) {
                    headers["Authorization"] =
                        "Bearer " +
                        token.replace(/^Bearer\\s+/i, "");
                }

                const response = await fetch(url, {
                    method: "GET",
                    headers: headers,
                    credentials: "include"
                });

                return {
                    status: response.status,
                    text: await response.text()
                };
            }""",
            {
                "url": url,
                "token": token
            }
        )

        print(f"[INFO] HTTP статус: {result['status']}")

        if result["status"] != 200:
            continue

        try:
            data = json.loads(result["text"])

        except json.JSONDecodeError:
            print("[ERROR] Ответ сервера не является JSON")
            continue

        if isinstance(data, dict) and "timetable" in data:
            return data

    raise RuntimeError(
        "Не удалось получить расписание"
    )


def load_old_schedule():
    """
    Загружает старое расписание.
    """

    if not OUTPUT_FILE.exists():
        return None

    try:
        with OUTPUT_FILE.open(
            "r",
            encoding="utf-8"
        ) as file:

            return json.load(file)

    except Exception as error:

        print(
            f"[ERROR] Не удалось прочитать "
            f"schedule.json: {error}"
        )

        return None


def save_schedule(data):
    """
    Сохраняет расписание в schedule.json.
    """

    temporary_file = OUTPUT_FILE.with_suffix(".tmp")

    temporary_file.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    temporary_file.replace(OUTPUT_FILE)


async def main():

    async with async_playwright() as p:

        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),

            headless=True,

            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ],
        )

        try:

            page = (
                context.pages[0]
                if context.pages
                else await context.new_page()
            )

            # Бесконечный цикл
            while True:

                print()
                print("=" * 60)

                print(
                    f"[INFO] Проверка расписания: "
                    f"{time.strftime('%d.%m.%Y %H:%M:%S')}"
                )

                print("=" * 60)

                try:

                    # Получаем новое расписание
                    new_schedule = await get_schedule(page)

                    # Получаем старое расписание
                    old_schedule = load_old_schedule()

                    # Сравниваем
                    if old_schedule == new_schedule:

                        print(
                            "[INFO] Расписание не изменилось."
                        )

                    else:

                        print(
                            "[UPDATE] Расписание изменилось!"
                        )

                        save_schedule(new_schedule)

                        print(
                            "[UPDATE] schedule.json обновлён."
                        )

                except Exception as error:

                    print(
                        f"[ERROR] Ошибка: {error}"
                    )

                print()
                print(
                    "[INFO] Следующая проверка "
                    "через 30 минут."
                )

                # Ждём 30 минут
                await asyncio.sleep(
                    UPDATE_INTERVAL
                )

        finally:

            await context.close()


if __name__ == "__main__":
    asyncio.run(main())