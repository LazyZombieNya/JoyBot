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
from telegram import Bot, InputMediaPhoto, InputMediaVideo


# токен Telegram-бота и ID чата
TELEGRAM_BOT_TOKEN = "7829262663:AAGNdKgCWpzsFtyVFinxuGsT8TeE1bexf34"
CHAT_ID = "-1001251629343"

# Инициализация бота
bot = Bot(token=TELEGRAM_BOT_TOKEN)

# URL сайта
BASE_URL = "https://joy.reactor.cc/all"
PROCESSED_POSTS = set()  # Здесь будут храниться ID уже отправленных постов

# Функция для парсинга одного поста
def parse_post(post):
    text_content = []
    media_content = []
    processed_images = set() #

    # Работаем с текстом (H3)
    for H3 in post.find_all('H3'):
        text = H3.get_text(strip=True)
        if text:
            text_content.append(text+"\n")

    # Работаем с текстом (p)
    for p in post.find_all('p'):
        text = p.get_text(strip=True)
        if text:
            text_content.append(text + "\n")

    # Работаем с <div class="prettyPhotoLink">
    for img_div in post.find_all('a', class_='prettyPhotoLink'):
        img_url = img_div.get('href')
        img_name = img_url.split('/')[-1]
        title = img_div.find("img").get("alt", "Нет описания")
        if img_name not in processed_images:
            media_content.append(("https:"+img_url, "photo", title))
            processed_images.add(img_name)

    # Работаем с <span class="video_holder">
    for video_span in post.find_all('span', class_='video_holder'):
        source_tag = video_span.find('source')
        if source_tag and source_tag.get('src'):
            img_url = video_span['src']

        print ('img_url '+img_url)
        img_name = img_url.split('/')[-1]
        title = video_span.find("img").get("alt", "Нет описания")
        if img_name not in processed_images:
            media_content.append(("https:" + img_url, "photo", title))
            processed_images.add(img_name)

            a_tag = zoomed_img_div.find('a')
            if a_tag and a_tag.get('href'):
                img_url = a_tag['href']

    # Работаем с <a> (ссылки)
    for a_tag in post.find_all('a'):
        href_url = a_tag['href']
        span_text = a_tag.find('span').get_text(strip=True) if a_tag.find('span') else None
        if span_text:
            text_content.append("["+span_text+"]("+href_url+")\n")

    # Работаем с видео
    for video_div in post.find_all('video'):
        video_url = video_div.get("data-src")
        title = video_div.get("title", "Нет описания")
        if video_url:
            media_content.append((video_url, "video", title))

    # Работаем с видео гиф
    for video_div in post.find_all('video_gif_source'):
        video_url = video_div.get("data-src")
        title = video_div.get("title", "Нет описания")
        if video_url:
            media_content.append((video_url, "video", title))

    return text_content, media_content

# Функция для отправки текста в Telegram
async def send_text_to_telegram(text_content):
    message = "".join(text_content)
    if message.strip():
        await bot.send_message(chat_id=CHAT_ID, text=message)
        print("Текстовое сообщение отправлено")


# Функция для отправки медиа-группы в Telegram
async def send_media_group(chat_id, media_content):
    media_group = []

    for url, media_type, caption in media_content:
        if media_type == "photo":
            media_group.append(InputMediaPhoto(media=url, caption=caption))
        elif media_type == "video":
            media_group.append(InputMediaVideo(media=url, caption=caption))

    if media_group:
        await bot.send_media_group(chat_id=chat_id, media=media_group)
        print("Медиа-группа отправлена")

# Основной цикл для проверки новых постов
async def monitor_website():
    while True:
        try:
            response = requests.get(BASE_URL)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                posts = soup.find_all("div", class_="postContainer")
                print(posts)
                for post in posts:
                    post_id = post.get("id")  # Уникальный идентификатор поста
                    if post_id not in PROCESSED_POSTS:
                        PROCESSED_POSTS.add(post_id)
                        text_content, media_content = parse_post(post)
                        #post_content = parse_post(post)

                        # Отправляем текст
                        if text_content:
                            await send_text_to_telegram(text_content)

                        # Отправляем медиа
                        if media_content:
                            await send_media_group(chat_id=CHAT_ID, media_content=media_content)

            else:
                print(f"Ошибка загрузки сайта: {response.status_code}")

        except Exception as e:
            print(f"Ошибка: {e}")

        # Задержка перед следующей проверкой
        await asyncio.sleep(60)  # Проверяем каждые 60 секунд


# Запуск программы
if __name__ == "__main__":
    asyncio.run(monitor_website())