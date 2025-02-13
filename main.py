import asyncio
import uuid
from collections import deque

import aiohttp
from bs4 import BeautifulSoup
from telegram.constants import ParseMode
from telegram import Bot, InputMediaPhoto, InputMediaVideo
from telegram.request import HTTPXRequest
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# токен Telegram-бота и ID чата
TELEGRAM_BOT_TOKEN = "7829262663:AAGNdKgCWpzsFtyVFinxuGsT8TeE1bexf34"
CHAT_ID = "-1001251629343"

HEADERS = {"User-Agent": "Mozilla/5.0"}

# Устанавливаем таймауты (убираем ошибку timeout)
request = HTTPXRequest(connect_timeout=60, read_timeout=60)
# Инициализация бота
bot = Bot(token=TELEGRAM_BOT_TOKEN, request=request)  # Увеличенный таймаут

# URL сайта
BASE_URL = "https://joy.reactor.cc/new"
#BASE_URL = "https://joy.reactor.cc/post/6027028"
# BASE_URL = "https://joy.reactor.cc/post/6008824"

# Списки
MAX_POSTS = 20
PROCESSED_POSTS = deque(
    maxlen=MAX_POSTS)  # Очередь с автоудалением старых записей # Здесь будут храниться ID уже отправленных постов

LIMIT_CAPTION = 1024  # Лимит символов описания поста телеграмм
LIMIT_TEXT_MSG = 4096  # Лимит символов для одного сообщения телеграмм
MAX_MEDIA_PER_GROUP = 10  # Лимит Telegram на медиа-группу


# Функция для парсинга одного поста
def parse_post(post):
    post_data = {"content": []}
    text_content = []

    # Работаем с текстом (H2)
    for H2 in post.find_all('h2'):
        text = H2.get_text(", ", strip=True)
        print(text)
        if text:
            post_data["content"].append({"id": str(uuid.uuid4()), "type": "h2", "data": text + "\n", "send": "yes"})

    # Работаем с текстом (H3)
    for H3 in post.find_all('h3'):
        text = H3.get_text(strip=True)
        print(text)
        if text:
            # post_data["content"].append({"type": "text", "data": text + "\n","send": "not"})
            text_content.append(text + " \n")

    # Работаем с текстом (p)
    for p in post.find_all('p'):
        text = p.get_text(", ", strip=True)
        if text:
            text_content.append(text + " \n")
        #parts = []

        # Проходим по всем элементам внутри <p>
        #for element in p.contents:
        #    if element.name == "a":  # Если это ссылка
        #        href = element.get("href")
        #        link_text = element.get_text(strip=True)

                # Ограничиваем длину текста внутри ссылки (например, 30 символов)
        #        if len(link_text) > 30:
        #            link_text = link_text[:27] + "..."  # Обрезаем текст и добавляем "..."

        #        full_link = f'<a href="{href}">\'{link_text}\'</a>'
        #        parts.append(full_link)
        #    elif isinstance(element, str):  # Если это обычный текст
        #        parts.append(element.strip())

        # Объединяем текстовые части и добавляем в список
        #if parts:
        #    text_content.append(", ".join(parts) + " \n")
    # TODO надо интегрировать  текст и ссылки вместе
    print(text_content)

    # Работаем с <a> (ссылки)
    # for a_tag in post.find_all('a'):
    #    href_url = a_tag['href']
    #    link_text = a_tag.get_text(strip=True)
    #    if link_text:
    #        post_data["content"].append({"id":str(uuid.uuid4()),"type": "link", "data": href_url, "text":link_text,"send": "not"})

    # Работаем с <div class="image">
    for img_div in post.find_all('div', class_='image'):
        img_tag = ""
        for video_span in img_div.find_all('span', class_='video_holder'):  # если тег image для видео
            img_tag = "video_holder"
            source_tag = video_span.find('source', type="video/mp4")
            video_url = source_tag.get('src')
            post_data["content"].append(
                {"id": str(uuid.uuid4()), "type": "video", "data": "https:" + video_url, "send": "not"})

        for video_gif_span in img_div.find_all('span', class_='video_gif_holder'):  # если тег image для gif
            img_tag = "video_gif_holder"
            img_url = video_gif_span.get('href')
            if img_url:
                post_data["content"].append(
                    {"id": str(uuid.uuid4()), "type": "video", "data": "https:" + img_url, "send": "not"})

        # Работаем с <div class="prettyPhotoLink">
        for full_div in img_div.find_all('a', class_='prettyPhotoLink'):
            img_tag = "prettyPhotoLink"
            img_url = full_div.get('href')
            # img_url=img_url_full.replace("/full/", "/")
            # img_name = img_url.split('/')[-1]

            # if text_content and (sum(len(text) for text in text_content) + len(h2_text)) < LIMIT_CAPTION:
            # title = h2_text + "\n" + "".join(text_content)

            post_data["content"].append(
                {"id": str(uuid.uuid4()), "type": "photo", "data": "https:" + img_url, "send": "not"})

        if img_tag == "":  # если тег image для картинок только
            img = img_div.find('img')
            if img and img.get('src'):
                img_url = img['src']
                post_data["content"].append(
                    {"id": str(uuid.uuid4()), "type": "photo", "data": "https:" + img_url, "send": "not"})

    # Работаем с <iframe это coub, youtube, vimeo
    for iframe in post.find_all('iframe'):
        iframe_url = iframe.get('src')
        post_data["content"].append(
            {"id": str(uuid.uuid4()), "type": "video_hosting", "data": iframe_url, "send": "not"})
        # text_content.append((iframe_url.split('?wmode=transparent&rel=0')[0]) + " \n")
        # media_content.append(( (iframe_url.split('?wmode=transparent&rel=0')[0]), "video_url", title))

    return post_data, text_content


# Функция для отправки текста в Telegram
async def send_text_to_telegram(text_content,caption):
    message = "".join(text_content)
    if message.strip():
        # Разделяем текст на части, если он превышает лимит
        parts = [message[i:i + LIMIT_TEXT_MSG] for i in range(0, len(message), LIMIT_TEXT_MSG)]

        for part in parts:
            await bot.send_message(chat_id=CHAT_ID, text=part)
            print("Часть текстового сообщения отправлена")


# Функция для отправки cылок в Telegram
async def send_link_to_telegram(link):
    if link:
        for url, media_type, caption in link:
            print("Отправка ссылки")
            await bot.send_message(chat_id=CHAT_ID, text="<a href=" + url + ">" + caption + "</a>",
                                   parse_mode=ParseMode.HTML)


# Функция для отправки медиа-группы в Telegram
async def send_post(chat_id, post_id, contents, text_content):
    photo_group = []
    id_photo = []
    video_group = []
    id_video = []

    # text_content = contents["text"]
    content_list = contents.get("content", [])  # Получаем список вложений
    title = next((item["data"] for item in content_list if item["type"] == "h2"), "")
    link_post = f'<a href="https://m.joyreactor.cc/post/{post_id}">Пост {post_id}</a> : '  # Эта будет ссылкой на пост

    # Ограничение размера группы перед отправкой
    # if len(media_content) > 20:  # Задайте разумный предел, например, 50 элементов
    #    print(f"Слишком много медиафайлов: {len(media_content)}. Отправка частями.")
    #    media_content = media_content[:30]  # Обрежьте до первых 50
    #    # print(media_content)
    if text_content and (sum(len(text) for text in text_content) + len(title + "\n") + len(link_post)) < LIMIT_CAPTION:
        caption = link_post + title + "\n" + "".join(text_content)
        text_content.clear()
    else:
        caption = link_post + title

    not_processed = True
    while not_processed:
        not_processed = False
        for index, content in enumerate(content_list):

            print(content_list)
            if content["send"] == "yes" or content["send"] == "close":
                continue  # Уже отправленные файлы пропускаем
            else:
                not_processed = True
            if content["type"] == "photo":
                match content["send"]:
                    case "not":
                        photo_group.append(
                            InputMediaPhoto(media=content["data"],
                                            caption=(caption if not photo_group else None),
                                            parse_mode="HTML"))  # caption только на первую картинку, так описание к группе будет
                        id_photo.append(content["id"])
                    case "err":
                        photo_group.append(
                            InputMediaPhoto(media=content["data"].replace("/full/", "/"),
                                            caption=(caption if not photo_group else None),
                                            # убираем /full/ картинка будет не высокого качества
                                            parse_mode="HTML"))  # caption только на первую картинку, так описание к группе будет
                        id_photo.append(content["id"])
            elif content["type"] == "video":
                if content["send"] == "not":
                    video_group.append(InputMediaVideo(media=content["data"],
                                                       caption=(caption if not photo_group else None),
                                                       parse_mode="HTML"))
                    id_video.append(content["id"])

            elif content["type"] == "video_hosting":
                if content["send"] == "not":
                    video_url = content["data"]
                    text = f"📺 Смотреть видео: {video_url} \n\n {caption} "
                    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
                    content["send"] = "yes"

            else:
                await bot.send_message(chat_id=chat_id, text=f"Не отправленный пост: {caption}")
                content["send"] = "close"

            if photo_group and ((len(photo_group) == MAX_MEDIA_PER_GROUP) or (index >= len(content_list) - 1)):

                try:
                    await bot.send_media_group(chat_id=chat_id, media=photo_group)
                    await asyncio.sleep(10)  # задержка в 10 секунд чтобы не срабатывал Flood control exceeded
                    for item in content_list:
                        if item["id"] in id_photo:  # Проверяем, есть ли ID в списке
                            item["send"] = "yes"
                except Exception as e:
                    print(f"Ошибка: {e}")
                    not_processed = False
                    for item in content_list:
                        if item["id"] in id_photo:  # Проверяем, есть ли ID в списке
                            if item["send"] == "not":
                                item["send"] = "err"
                            else:
                                item["send"] = "close"
                photo_group.clear()
                id_photo.clear()

            if video_group and ((len(video_group) == MAX_MEDIA_PER_GROUP) or (index >= len(content_list) - 1)):

                try:
                    await bot.send_media_group(chat_id=chat_id, media=video_group)
                    await asyncio.sleep(10)  # задержка в 10 секунд чтобы не срабатывал Flood control exceeded
                    for item in content_list:
                        if item["id"] in id_video:  # Проверяем, есть ли ID в списке
                            item["send"] = "yes"
                except Exception as e:
                    print(f"Ошибка: {e}")
                    not_processed = False
                    for item in content_list:
                        if item["id"] in id_video:  # Проверяем, есть ли ID в списке
                            if item["send"] == "not":
                                item["send"] = e
                            else:
                                item["send"] = "close"
                video_group.clear()
                id_video.clear()

            if text_content:
                await send_text_to_telegram(text_content) #Отправляем длинные тексты

        print(not_processed)
        await asyncio.sleep(10)  # задержка в 2 секунды чтобы не срабатывал Flood control exceeded


async def fetch_html(url):
    """Асинхронный запрос к сайту."""
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        async with session.get(url) as response:
            if response.status == 200:
                return await response.text()
            else:
                print(f"Ошибка загрузки сайта: {response.status}")
                return []


# Основной цикл для проверки новых постов
async def monitor_website():
    post_id = 0

    while True:
        try:
            html = await fetch_html(BASE_URL)
            soup = BeautifulSoup(html, "html.parser")
            posts = soup.find_all("div", class_="postContainer")
            for post in posts:
                post_id_full = post.get("id")  # Уникальный идентификатор поста
                post_id = post_id_full.split('postContainer')[-1].strip('"')
                if post_id not in PROCESSED_POSTS:
                    print(post_id)
                    post_data, text_content = parse_post(post)

                    # Отправляем данные с поста
                    if post_data:
                        await send_post(chat_id=CHAT_ID, post_id=post_id, contents=post_data, text_content=text_content)

                    PROCESSED_POSTS.append(post_id)  # помечаем что пост отправлен
                    print(PROCESSED_POSTS)
        except Exception as e:
            print(f"Ошибка: {e}")
            print("Ошибка в посте:" + post_id)

        # Задержка перед следующей проверкой
        await asyncio.sleep(60)  # Проверяем каждые 60 секунд


# Запуск программы
if __name__ == "__main__":
    asyncio.run(monitor_website())
