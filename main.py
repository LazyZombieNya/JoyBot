import asyncio
from bs4 import BeautifulSoup
import requests
from telegram.constants import ParseMode
from telegram import Bot, InputMediaPhoto, InputMediaVideo
from telegram.request import HTTPXRequest
import time


# токен Telegram-бота и ID чата
TELEGRAM_BOT_TOKEN = "7829262663:AAGNdKgCWpzsFtyVFinxuGsT8TeE1bexf34"
CHAT_ID = "-1001251629343"


# Устанавливаем таймауты (убираем ошибку timeout)
request = HTTPXRequest(connect_timeout=60, read_timeout=60)
# Инициализация бота
bot = Bot(token=TELEGRAM_BOT_TOKEN, request=request)# Увеличенный таймаут

# URL сайта
BASE_URL = "https://joy.reactor.cc/new"
#BASE_URL = "https://joy.reactor.cc/post/5880111"
#BASE_URL = "https://joy.reactor.cc/post/6008824"
PROCESSED_POSTS = set()  # Здесь будут храниться ID уже отправленных постов

# Функция для парсинга одного поста
def parse_post(post):
    text_content = []
    media_content = []
    h2_text="" #теги поста
    processed_images = set() #Список отправленных картинок

    #print(post)

    # Работаем с текстом (H2)
    for H2 in post.find_all('h2'):
        text = H2.get_text(", ",strip=True)
        print(text)
        if text:
            h2_text=text + " \n"

    # Работаем с текстом (H3)
    for H3 in post.find_all('h3'):
        text = H3.get_text(strip=True)
        print( text)
        if text:
            text_content.append(text+" \n")

    # Работаем с текстом (p)
    for p in post.find_all('p'):
        text = p.get_text(", ",strip=True)
        print( text)
        if text:
            text_content.append(text + " \n")

    # Работаем с <div class="prettyPhotoLink">
    for img_div in post.find_all('a', class_='prettyPhotoLink'):
        img_url_full = img_div.get('href')
        img_url=img_url_full.replace("/full/", "/")
        img_name = img_url.split('/')[-1]
        if h2_text:
            title=h2_text
        else:
            title = img_div.find("img").get("alt", "Нет тегов")
        if img_name not in processed_images:
            media_content.append(("https:"+img_url, "photo", title))
            processed_images.add(img_name)


    # Работаем с <span class="video_holder">
    for video_span in post.find_all('span', class_='video_holder'):
        source_tag = video_span.find('source', type="video/mp4")
        video_url = source_tag.get('src')
        if h2_text:
            title = h2_text
        else:
            title = video_span.find("img").get("alt", "Нет тегов")
        media_content.append(("https:" + video_url, "video", title))

    # Работаем с <a class="video_gif_source"
    for gif_a in post.find_all('a', class_='video_gif_source'):
        gif_url = gif_a.get('href')
        if h2_text:
            title = h2_text
        else:
            title = gif_a.get("title", "Нет тегов")
        media_content.append(("https:" + gif_url, "video", title))

    # Работаем с <iframe это coub
    for iframe in post.find_all('iframe'):
        iframe_url = iframe.get('src')
        title = iframe.get("title", "Нет тегов")
        text_content.append((iframe_url.split('?wmode=transparent&rel=0')[0]) + " \n")
        #media_content.append(( (iframe_url.split('?wmode=transparent&rel=0')[0]), "video_url", title))


    # Работаем с <a> (ссылки)
    for a_tag in post.find_all('a'):
        href_url = a_tag['href']
        span_text = a_tag.find('span').get_text(strip=True) if a_tag.find('span') else None
        if span_text:
            print("Ссылка")
            text_content.append("["+span_text+"]("+href_url+")\n")
           # link.append((href_url, "link", span_text))

    return text_content, media_content

# Функция для отправки текста в Telegram
async def send_text_to_telegram(text_content):
    message = "".join(text_content)
    if message.strip():
        # Лимит символов для одного сообщения
        limit = 4096
        # Разделяем текст на части, если он превышает лимит
        parts = [message[i:i + limit] for i in range(0, len(message), limit)]

        for part in parts:
            await bot.send_message(chat_id=CHAT_ID, text=part)
            print("Часть текстового сообщения отправлена")

# Функция для отправки cылок в Telegram
async def send_link_to_telegram(link):
    if link:
        for url, media_type, caption in link:
            print("Отправка ссылки")
            await bot.send_message(chat_id=CHAT_ID, text="<a href="+url+">"+caption+"</a>", parse_mode=ParseMode.HTML)


# Функция для отправки медиа-группы в Telegram
async def send_media_group(chat_id, post_id, media_content):
    MAX_MEDIA_PER_GROUP = 10  # Лимит Telegram на медиа-группу
    link_post = f'\n<a href="https://joy.reactor.cc/post/{post_id}">Пост {post_id}</a>'


    # Ограничение размера группы перед отправкой
    if len(media_content) > 20:  # Задайте разумный предел, например, 50 элементов
        print(f"Слишком много медиафайлов: {len(media_content)}. Отправка частями.")
        media_content = media_content[:30]  # Обрежьте до первых 50
        #print(media_content)

    # Разбиваем media_content на группы по 10 элементов
    for i in range(0, len(media_content), MAX_MEDIA_PER_GROUP):
        photo_group = []
        video_group = []
        batch = media_content[i:i + MAX_MEDIA_PER_GROUP]

        for url, media_type, caption in batch:
            if media_type == "photo":
                photo_group.append(InputMediaPhoto(media=url, caption=(caption+link_post if not photo_group else None), parse_mode="HTML"))#caption только на первую картинку, так описание к группе будет
            elif media_type == "video":
                await bot.send_video(chat_id=chat_id,video=url, caption=caption+link_post, parse_mode="HTML")
            #print(url)

        if photo_group:
            await bot.send_media_group(chat_id=chat_id, media=photo_group)
            print(f"Фото-группа отправлена: {len(photo_group)} элементов")

    time.sleep(2)  # задержка в 2 секунды чтобы не срабатывал Flood control exceeded


# Основной цикл для проверки новых постов
async def monitor_website():
    while True:
        try:
            response = requests.get(BASE_URL)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                posts = soup.find_all("div", class_="postContainer")
                #print(posts)
                for post in posts:
                    post_id_full = post.get("id")  # Уникальный идентификатор поста
                    post_id=post_id_full.split('postContainer')[-1].strip('"')
                    if post_id not in PROCESSED_POSTS:
                        print(post_id)
                        text_content, media_content= parse_post(post)

                        # Отправляем текст
                        if text_content:
                            await send_text_to_telegram(text_content)

                        # Отправляем медиа
                        if media_content:
                            await send_media_group(chat_id=CHAT_ID, post_id=post_id,media_content=media_content)

                        #if link:
                         #   await send_link_to_telegram(link)
                        PROCESSED_POSTS.add(post_id) #помечаем что пост отправлен

            else:
                print(f"Ошибка загрузки сайта: {response.status_code}")

        except Exception as e:
            print(f"Ошибка: {e}")

        # Задержка перед следующей проверкой
        await asyncio.sleep(60)  # Проверяем каждые 60 секунд


# Запуск программы
if __name__ == "__main__":
    asyncio.run(monitor_website())