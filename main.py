import asyncio
import os
from dotenv import load_dotenv

import uuid
from collections import deque, Counter, defaultdict
from urllib.parse import urlparse
from PIL import Image
from io import BytesIO

import html
import aiofiles
import aiohttp
from bs4 import BeautifulSoup
from telegram.constants import ParseMode
from telegram import Bot, InputMediaPhoto, InputMediaVideo, InputMediaAnimation, InputFile
from telegram.request import HTTPXRequest

load_dotenv()  # Загружаем переменные из .env

# токен Telegram-бота и ID чата
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_V = os.getenv("TELEGRAM_CHAT_V")
TELEGRAM_CHAT_PL = os.getenv("TELEGRAM_CHAT_PL")

# URL сайтов
URLS_V = os.getenv("URLS_V", "").split(",")
URLS_PL = os.getenv("URLS_PL", "").split(",")
URLS = URLS_V + URLS_PL


if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_V:
    raise ValueError("Ошибка: Не установлены переменные окружения!")

HEADERS = {"User-Agent": "Mozilla/5.0"}

# Устанавливаем таймауты (убираем ошибку timeout)
request = HTTPXRequest(connect_timeout=60, read_timeout=60)
# Инициализация бота
bot = Bot(token=TELEGRAM_BOT_TOKEN, request=request)  # Увеличенный таймаут



# Ограничения Telegram
LIMIT_CAPTION = 1024  # Лимит символов описания поста телеграмм
LIMIT_TEXT_MSG = 4096  # Лимит символов для одного сообщения телеграмм
MAX_MEDIA_PER_GROUP = 10  # Лимит Telegram на медиа-группу
MAX_SIZE_MB = 10  # Максимальный размер фото в MB
MAX_TOTAL_DIMENSIONS = 10000  # Максимальная сумма ширины и высоты
MAX_ASPECT_RATIO = 20  # Максимальное соотношение сторон
MAX_WIDTH_IMG = 5000  # Максимальные размеры изображений, которые не выходят за лимиты
MAX_HEIGHT_IMG = 5000

# Списки
MAX_POSTS = 30
PROCESSED_POSTS = defaultdict(lambda: deque(maxlen=MAX_POSTS))  # Словарь с обработанными постами (отдельно для каждого сайта) с авто удалением старых записей
DATA_FOLDER = "temp_data"  # Папка где хранятся временно скачанные файлы
UNWANTED_TAGS = {"Ватные вбросы", "Я Ватник"}  # Нежелательные теги, посты с этим тегом будут пропущены


# Функция для парсинга одного поста из joy.reactor.cc
def parse_joy_post(post):
    post_data = {"content": []}
    text_content = []

    # Работаем с текстом (H2), теги
    for H2 in post.find_all('h2'):
        text = H2.get_text(", ", strip=True)
        if text:
            if any(tag in text for tag in UNWANTED_TAGS):  # Проверка поста на не желательные теги
                post_data.clear()
                return {}, []  # Завершаем функцию, пост не обрабатывается
            else:
                post_data["content"].append(
                    {"id": str(uuid.uuid4()), "type": "h2", "data": html.escape(text) + "\n", "send": "yes"})

    # Работаем с текстом (H3)
    for H3 in post.find_all('h3'):
        text = H3.get_text(strip=True)
        if text:
            # post_data["content"].append({"type": "text", "data": text + "\n","send": "not"})
            text_content.append(html.escape(text) + " \n")

    # Работаем с текстом (p) и ссылками внутри него a href
    for p in post.find_all('p'):
        parts = []

        # Проходим по всем элементам внутри <p>
        for element in p.contents:
            if element.name == "a":  # Если это ссылка
                href = element.get("href")
                link_text = element.get_text(strip=True)

                # Ограничиваем длину текста внутри ссылки (например, 30 символов)
                if len(link_text) > 30:
                    link_text = link_text[:27] + "..."  # Обрезаем текст и добавляем "..."

                full_link = f'<a href="{html.escape(href)}">{html.escape(link_text)}</a>'
                parts.append(full_link)
            elif isinstance(element, str):  # Если это обычный текст
                parts.append(html.escape(element.strip()))
            else:
                # Если это тег с вложенным текстом (например, <b>, <i>, <u> и т.д.), извлекаем его текст
                parts.append(html.escape(element.get_text(strip=True)))

        # Объединяем текстовые части и добавляем в список
        if parts:
            text_content.append(", ".join(parts) + " \n")

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
            source_tag = video_gif_span.find('source', type="video/mp4")
            if source_tag:
                img_url = source_tag.get('src')
                type_content = "video"
            else:
                a_tag = video_gif_span.find('a')
                img_url = a_tag.get('href')
                type_content = "gif"
            if img_url:
                post_data["content"].append(
                    {"id": str(uuid.uuid4()), "type": type_content, "data": "https:" + img_url, "send": "not"})

        # Работаем с <div class="prettyPhotoLink">
        for full_div in img_div.find_all('a', class_='prettyPhotoLink'):
            img_tag = "prettyPhotoLink"
            img_url = full_div.get('href')
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

    return post_data, text_content


# Функция для отправки текста в Telegram
async def send_text_to_telegram(chat_id, text_content, caption):
    message = "".join(text_content) + "\n" + caption
    if message.strip():
        # Разделяем текст на части, если он превышает лимит
        parts = [message[i:i + LIMIT_TEXT_MSG] for i in range(0, len(message), LIMIT_TEXT_MSG)]

        for part in parts:
            try:
                await bot.send_message(chat_id=chat_id, text=part, parse_mode=ParseMode.HTML)
            except Exception as e:
                print(f"Ошибка текста: {e}")
                await bot.send_message(chat_id=chat_id, text=html.escape(part), parse_mode=ParseMode.HTML)
            await asyncio.sleep(30)  # задержка в 30 секунд чтобы не срабатывал Flood control exceeded


# Функция для отправки медиа-группы в Telegram
async def send_post(chat_id, post_id, contents, text_content):
    photo_group = []
    id_photo = []
    video_group = []
    id_video = []
    gif_group = []
    id_gif = []

    content_list = contents.get("content", [])  # Получаем список вложений
    title = next((item["data"] for item in content_list if item["type"] == "h2"),
                 "")  # теги, которые находятся в заголовке H2
    link_post = f'<a href="https://m.joyreactor.cc/post/{post_id}">Пост {post_id}</a> : '  # Эта будет ссылкой на пост
    type_counts = Counter(item['type'] for item in content_list)  # Считаем количество типов файлов в json

    # Ограничение размера группы перед отправкой
    # if len(media_content) > 20:  # Задайте разумный предел, например, 50 элементов
    #    print(f"Слишком много медиафайлов: {len(media_content)}. Отправка частями.")
    #    media_content = media_content[:30]  # Обрежьте до первых 50

    if text_content and (sum(len(text) for text in text_content) + len(title + "\n") + len(link_post)) < LIMIT_CAPTION:
        caption = link_post + title + "\n" + "".join(text_content)
        text_content.clear()
    else:
        caption = link_post + title

    count_send_photo = 0
    count_send_video = 0
    count_send_gif = 0
    not_processed = True  # Флаг отслеживания все ли обработано в посте
    everything_sent = True  # Флаг для отслеживания все ли отправлено
    while not_processed:
        not_processed = False
        for index, content in enumerate(content_list):
            if content["send"] == "yes":
                continue  # Уже отправленные файлы пропускаем
            elif content["send"] == "close":
                everything_sent = False
                continue  # Те файлы, что так и не получилось отправить так же пропускаем, но помечаем что не все отправлено
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

                        # photo_group.append( # убираем /full/ картинка будет не высокого качества
                        #    InputMediaPhoto(media=content["data"].replace("/full/", "/"),
                        #                    caption=(caption if not photo_group else None),
                        #
                        #                    parse_mode="HTML"))  # caption только на первую картинку, так описание к группе будет

                        # Качаем файл если по ссылке не отправляется
                        photo_url = content["data"]
                        ext = get_file_extension(photo_url)
                        local_filename = f"temp_image_{content['id']}.{ext}"
                        downloaded_file = await download_media(photo_url, local_filename)
                        if downloaded_file:
                            with open(downloaded_file, 'rb') as file:
                                photo_group.append(InputMediaPhoto(
                                    media=file.read(),
                                    caption=(caption if not photo_group else None),
                                    parse_mode="HTML"))
                        else:
                            content["send"] = "close"
                        id_photo.append(content["id"])

            elif content["type"] == "video":
                match content["send"]:
                    case "not":
                        video_group.append(InputMediaVideo(media=content["data"],
                                                           caption=(caption if not video_group else None),
                                                           parse_mode="HTML"))
                        id_video.append(content["id"])
                    case "err":  # Пробуем качать видео на диск
                        video_url = content["data"]
                        ext = get_file_extension(video_url)
                        local_filename = f"temp_video_{content['id']}.{ext}"
                        downloaded_file = await download_media(video_url, local_filename)
                        if downloaded_file:
                            video_group.append(InputMediaVideo(
                                media=open(downloaded_file, 'rb'),
                                caption=(caption if not video_group else None),
                                parse_mode="HTML"
                            ))
                        else:
                            content["send"] = "close"
                        id_video.append(content["id"])

            elif content["type"] == "gif":

                match content["send"]:
                    case "not":
                        gif_group.append(InputMediaAnimation(media=content["data"],
                                                             caption=(caption if not gif_group else None),
                                                             parse_mode="HTML"))
                        id_gif.append(content["id"])
                    case "err":  # Пробуем качать видео на диск
                        video_url = content["data"]
                        ext = get_file_extension(video_url)
                        local_filename = f"temp_video_{content['id']}.{ext}"
                        downloaded_file = await download_media(video_url, local_filename)
                        if downloaded_file:
                            gif_group.append(InputMediaAnimation(
                                media=open(downloaded_file, 'rb'),
                                caption=(caption if not gif_group else None),
                                parse_mode="HTML"
                            ))
                        else:
                            content["send"] = "close"
                        id_gif.append(content["id"])
            elif content["type"] == "video_hosting":
                if content["send"] == "not":
                    video_url = content["data"]
                    text = f'<a href="{video_url}">📺 Смотреть видео</a> \n\n {caption}'

                    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
                    await asyncio.sleep(10)  # задержка в 10 секунд чтобы не срабатывал Flood control exceeded
                    content["send"] = "yes"
            else:
                await bot.send_message(chat_id=chat_id, text=f"Не известный контент {link_post} {content['data']}")
                content["send"] = "close"

            if photo_group and (
                    (len(photo_group) == MAX_MEDIA_PER_GROUP) or (
                    len(photo_group) >= (type_counts.get('photo', 0) - count_send_photo)) or (
                            index == len(content_list) - 1)):
                try:
                    await bot.send_media_group(chat_id=chat_id, media=photo_group)
                    for item in content_list:
                        if item["id"] in id_photo:  # Проверяем, есть ли ID в списке
                            item["send"] = "yes"
                            count_send_photo += 1
                except Exception as e:
                    print(f"Ошибка у {post_id}: {e}")
                    # not_processed = True
                    for item in content_list:
                        if item["id"] in id_photo:  # Проверяем, есть ли ID в списке
                            if item["send"] == "not":
                                item["send"] = "err"
                            else:
                                item["send"] = "close"
                                count_send_photo += 1
                await asyncio.sleep(40)  # задержка в 30 секунд чтобы не срабатывал Flood control exceeded
                photo_group.clear()
                id_photo.clear()

            if video_group and (
                    (len(video_group) == MAX_MEDIA_PER_GROUP) or (
                    len(video_group) == type_counts.get('video', 0) - count_send_video) or (
                            index == len(content_list) - 1)):
                try:
                    await bot.send_media_group(chat_id=chat_id, media=video_group)
                    for item in content_list:
                        if item["id"] in id_video:  # Проверяем, есть ли ID в списке
                            item["send"] = "yes"
                            count_send_video += 1
                except Exception as e:
                    print(f"Ошибка у {post_id}: {e}")
                    # not_processed = True
                    for item in content_list:
                        if item["id"] in id_video:  # Проверяем, есть ли ID в списке
                            if item["send"] == "not":
                                item["send"] = "err"
                            else:
                                item["send"] = "close"
                                count_send_video += 1
                await asyncio.sleep(30)  # задержка в 30 секунд чтобы не срабатывал Flood control exceeded
                video_group.clear()
                id_video.clear()

            if gif_group and ((len(gif_group) == MAX_MEDIA_PER_GROUP) or (
                    len(gif_group) == type_counts.get('gif', 0) - count_send_gif) or (index == len(content_list) - 1)):
                try:
                    for gif_file in gif_group:
                        await bot.send_animation(chat_id=chat_id, animation=gif_file.media, caption=caption,
                                                 parse_mode="HTML")
                    for item in content_list:
                        if item["id"] in id_gif:  # Проверяем, есть ли ID в списке
                            item["send"] = "yes"
                            count_send_gif += 1
                except Exception as e:
                    print(f"Ошибка у {post_id}: {e}")
                    # not_processed = True
                    for item in content_list:
                        if item["id"] in id_gif:  # Проверяем, есть ли ID в списке
                            if item["send"] == "not":
                                item["send"] = "err"
                            else:
                                item["send"] = "close"
                                count_send_gif += 1
                await asyncio.sleep(30)  # задержка в 30 секунд чтобы не срабатывал Flood control exceeded
                gif_group.clear()
                id_gif.clear()
        if text_content:
            await send_text_to_telegram(chat_id, text_content, caption)  # Отправляем длинные тексты
            text_content.clear()
    if not everything_sent:
        await bot.send_message(chat_id=chat_id,
                               text=link_post + "Не все удалось отправить, чтобы посмотреть нажмите на пост",
                               parse_mode=ParseMode.HTML)
    await clear_data_folder()  # Удаляем скачанные файлы


# Удаляет все файлы в папке DATA_FOLDER, если они есть.
async def clear_data_folder():
    if os.path.exists(DATA_FOLDER):
        for file in os.listdir(DATA_FOLDER):
            file_path = os.path.join(DATA_FOLDER, file)
            try:
                os.remove(file_path)
            except Exception as e:
                print(f"Ошибка при удалении {file_path}: {e}")


# Скачивает медиафайлы на диск
async def download_media(url, filename):
    headers = {  # Делаем шапку чтобы не ругался и не блокировали доступ к файлам
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://joy.reactor.cc/"
    }

    ext = filename.split('.')[-1].lower()  # Определяем расширение файла
    os.makedirs(DATA_FOLDER, exist_ok=True)  # Создаем папку Data, если её нет
    file_path = os.path.join(DATA_FOLDER, filename)
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                file_bytes = await response.read()  # Скачиваем файл как байты
                # Если файл - изображение, меняем размер
                if ext in ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp']:
                    # Открываем изображение с помощью PIL
                    with Image.open(BytesIO(file_bytes)) as img:
                        # Масштабируем изображение, сохраняя пропорции
                        # img.thumbnail((MAX_WIDTH_IMG, MAX_HEIGHT_IMG))
                        width, height = img.size

                        # Проверяем по ограничениям Telegram
                        if (width + height) > MAX_TOTAL_DIMENSIONS or (
                                max(width, height) / min(width, height)) > MAX_ASPECT_RATIO:
                            # Вычисляем коэффициент масштабирования
                            scale_factor = min(MAX_TOTAL_DIMENSIONS / (width + height),
                                               MAX_ASPECT_RATIO / (max(width, height) / min(width, height)))
                            new_width = int(width * scale_factor)
                            new_height = int(height * scale_factor)

                            # Масштабируем изображение методом Lanczos
                            img = img.resize((new_width, new_height), Image.LANCZOS)

                        # Проверяем ограничение по размеру (10MB)
                        img_bytes = BytesIO()
                        img.save(img_bytes, format=ext)
                        while len(img_bytes.getvalue()) > (MAX_SIZE_MB * 1024 * 1024):
                            img = img.resize((img.width // 2, img.height // 2), Image.LANCZOS)
                            img_bytes = BytesIO()
                            img.save(img_bytes, format=ext)

                        # Сохраняем на диск
                        img.save(file_path)
                else:
                    # Для других файлов (например, видео) сохраняем без изменений
                    async with aiofiles.open(file_path, 'wb') as file:
                        #    await file.write(await response.read())
                        await file.write(file_bytes)
                return file_path
            else:
                print(f"Ошибка загрузки: {response.status}")
    return None


# Узнаем какого разрешения файл по ссылке
def get_file_extension(url):
    parsed_url = urlparse(url)
    path = parsed_url.path  # Достаем путь из ссылки
    extension = path.split('.')[-1]  # Берем последнее слово после точки
    return extension.lower()  # Возвращаем в нижнем регистре


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
            for url in URLS:
                html = await fetch_html(url)
                if url in URLS_V:
                    chat_id = TELEGRAM_CHAT_V
                elif url in URLS_PL:
                    chat_id = TELEGRAM_CHAT_PL
                soup = BeautifulSoup(html, "html.parser")
                posts = soup.find_all("div", class_="postContainer")
                for post in posts:
                    post_id_full = post.get("id")  # Уникальный идентификатор поста
                    post_id = post_id_full.split('postContainer')[-1].strip('"')
                    if post_id not in PROCESSED_POSTS[url]:
                        post_data, text_content = parse_joy_post(post)
                        # Отправляем данные с поста
                        if post_data:

                            await send_post(chat_id=chat_id, post_id=post_id, contents=post_data,
                                            text_content=text_content)

                        PROCESSED_POSTS[url].append(post_id)  # помечаем что пост отправлен
        except Exception as e:
            print(f"Ошибка: {e}")
            print("Ошибка в посте:" + post_id)

        # Задержка перед следующей проверкой
        await asyncio.sleep(30)  # Проверяем каждые 60 секунд


# Запуск программы
if __name__ == "__main__":
    asyncio.run(monitor_website())
