#Ишем все <span> и если есть закрытие </span> то делаем перенос строки
#<div class="image">  src= ссылка на картинку  title= теги
#<div class="image zoomed-image" класс где картинку можно увеличить <a href= ссылка на картинку <img alt= теги
#< a href =  ссылка между <span> стоит текст ссылки
#<div class="single"> контент с видео
#    < video data - src = ссылка на видео
#    title= теги
import asyncio

from bs4 import BeautifulSoup
import requests
import time
import telegram

# токен Telegram-бота и ID чата
TELEGRAM_BOT_TOKEN = "7829262663:AAGNdKgCWpzsFtyVFinxuGsT8TeE1bexf34"
CHAT_ID = "-1001251629343"

# Инициализация бота
bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)

# URL сайта
BASE_URL = "https://joyreactor.cc/"
PROCESSED_POSTS = set()  # Здесь будут храниться ID уже отправленных постов


# Функция для парсинга одного поста
def parse_post(post):
    post_content = []
    # Парсинг текста
    for span in post.find_all("span"):
        text = span.get_text(strip=True)
        if text:
            post_content.append(text + "\n")

    # Парсинг изображений
    for image in post.find_all("div", class_="image"):
        img_tag = image.find("img")
        if img_tag:
            img_url = img_tag.get("src")
            title = img_tag.get("title", "Нет тегов")
            post_content.append(f"Изображение: {img_url} (Теги: {title})\n")

    # Парсинг увеличиваемых изображений
    for zoomed_image in post.find_all("div", class_="image zoomed-image"):
        a_tag = zoomed_image.find("a")
        if a_tag:
            img_url = a_tag.get("href")
            title = a_tag.find("img").get("alt", "Нет тегов")
            post_content.append(f"Увеличиваемое изображение: {img_url} (Теги: {title})\n")

    # Парсинг ссылок
    for a_tag in post.find_all("a", href=True):
        link_text = a_tag.get_text(strip=True)
        link_url = a_tag["href"]
        if link_text:
            post_content.append(f"Ссылка: {link_text} ({link_url})\n")

    # Парсинг видео
    for video in post.find_all("video"):
        video_url = video.get("data-src")
        title = video.get("title", "Нет тегов")
        if video_url:
            post_content.append(f"Видео: {video_url} (Теги: {title})\n")

    return post_content


# Функция для отправки сообщения в Telegram
async def send_to_telegram(post_content):
    message = "\n".join(post_content)
    if message.strip():
        await bot.send_message(chat_id=CHAT_ID, text=message)
        print("Сообщение отправлено")


# Основной цикл для проверки новых постов
async def monitor_website():
    while True:
        try:
            response = requests.get(BASE_URL)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                posts = soup.find_all("div", class_="post-content")

                for post in posts:
                    post_id = hash(str(post))  # Уникальный идентификатор поста
                    if post_id not in PROCESSED_POSTS:
                        PROCESSED_POSTS.add(post_id)
                        post_content = parse_post(post)
                        await send_to_telegram(post_content)

            else:
                print(f"Ошибка загрузки сайта: {response.status_code}")

        except Exception as e:
            print(f"Ошибка: {e}")

        # Задержка перед следующей проверкой
        await asyncio.sleep(60)  # Проверяем каждые 60 секунд


# Запуск программы
if __name__ == "__main__":
    asyncio.run(monitor_website())