# JoyBot

Телеграм-бот для автоматической отправки свежих постов с сайта joy.reactor.cc в указанные Telegram-чаты.

## 🚀 Возможности

- Парсинг новых постов с сайта joy.reactor.cc.
- Отправка постов в разные Telegram-чаты по разным URL.
- Гибкая настройка через файл `.env`.

## 📦 Установка

1. Клонируйте репозиторий:

   ```bash
   git clone https://github.com/LazyZombieNya/JoyBot.git
   cd JoyBot
2. Установите зависимости:

   ```bash
   pip install -r requirements.txt
   ```

3. Создайте файл `.env` в корне проекта и добавьте в него:

   ```env
    TELEGRAM_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
    URLS_V=https://joy.reactor.cc/new
    URLS_PL=https://joy.reactor.cc/tag/Anime
    TELEGRAM_CHAT_V=YOUR_TG_CHAT_V
    TELEGRAM_CHAT_PL=YOUR_TG_CHAT_PL

   ```

## ▶️ Запуск

Запустите бота:

```bash
python main.py
```

Или используйте команду `python main.py` в терминале.
