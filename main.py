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
    processed_images = set()

    # Работаем с текстом (<span>)
    for span_tag in post.find_all('span'):
        span_text = span_tag.get_text(strip=True)
        if span_text:
            post_content.append(f"Текст: {span_text}")

    # Работаем с <div class="image zoomed-image">
    for zoomed_img_div in post.find_all('div', class_='image zoomed-image'):
        a_tag = zoomed_img_div.find('a')
        if a_tag and a_tag.get('href'):
            img_url = a_tag['href']
            img_name = img_url.split('/')[-1]
            if img_name not in processed_images:
                post_content.append(f"Увеличиваемая картинка: {img_url}")
                processed_images.add(img_name)

    # Работаем с <div class="image">
    for img_div in post.find_all('div', class_='image'):
        img_tag = img_div.find('img')
        if img_tag and img_tag.get('src'):
            img_url = img_tag['src']
            img_name = img_url.split('/')[-1]
            if img_name not in processed_images:
                post_content.append(f"Картинка: {img_url}")
                processed_images.add(img_name)

    # Работаем с <a> (ссылки)
    for a_tag in post.find_all('a'):
        span_text = a_tag.find('span').get_text(strip=True) if a_tag.find('span') else None
        if span_text:
            post_content.append(f"Ссылка: {span_text} ({a_tag['href']})")

    # Работаем с видео
    for video_div in post.find_all('div', class_='ant-spin-nested-loading'):
        video_tag = video_div.find('video')
        if video_tag and video_tag.get('data-src'):
            post_content.append(f"Видео: {video_tag['data-src']}")

    for single_div in post.find_all('div', class_='single'):
        video_tag = single_div.find('video')
        if video_tag and video_tag.get('data-src'):
            post_content.append(f"Видео: {video_tag['data-src']}")

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