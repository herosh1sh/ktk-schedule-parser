# ktk-schedule-parser
Данная программа позволяет парсить расписание и потом через Telegram-бота выводить это расписание, работает парсер на простом методе с запуском браузера (без окна), который считывает информацию с сайта колледжа https://ktk-45.ru/api/v1/timetable/*вашаГруппа*?time=17xxxxxxxxxxx (парсер сам подставит значение) и потом записывает расписание в отдельный файл под названием schedule.json и обновляет его каждые 30 минут, чтобы посмотреть, есть ли изменения в расписании.
**В ссылку с расписанием вставьте свой номер группы**

## Установка бота на Windows
Первым делом, нужно установить зависимости командой (используются aiogram, python-dotenv и playwright):
```
pip install -r requirements.txt
```
Уже после установки зависимостей прописываем данную команду для установки браузера playwright:
```
playwright install --with-deps chromium
```
После установки всех зависимостей, создаем .env файл и прописываем туда:
```
BOT_TOKEN=ваш_токен
```
Как прописали данные действия, идем в bot.py и запускаем его командой:
```
python bot.py
```
После запуска проверяем бота и радуемся, что всё работает 🤙

## Установка бота на VPS-сервер (Ubuntu Server, Debian)
Сначала обновим все пакеты сервера:
```
apt get update && apt get upgrade
```
Как обновили все пакеты, с помощью **git clone** копируем наш репозиторий и устанавливаем Docker(все команды выполняйте поочередно):
```
git clone https://github.com/herosh1sh/ktk-schedule-parser.git
sudo apt update
sudo apt install ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update

sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```
Переходим в корневую папку проекта:
```
cd ktk-schedule-parser
```
Создаем там .env и прописываем данное значение:
```
BOT_TOKEN=ваш_токен
```
После выполняем данные команды для Docker'а:
```
docker compose up -d -build && docker compose logs -f -t
```
После выполненных всех команд бот у вас должен запуститься, а парсер выдать в логах сообщение, что ему удалось спрасить расписание в .json файл
