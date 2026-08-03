import asyncio
import html
import logging
import os
import pickle
import sys
import uuid
from collections import deque, Counter, defaultdict
from urllib.parse import urlparse, urlunparse

import aiofiles
import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import InputMediaPhoto, InputMediaVideo, InputMediaAnimation
from telegram.constants import ParseMode
from telegram.ext import Application, ApplicationBuilder, ContextTypes, AIORateLimiter
from telegram.request import HTTPXRequest
from telegram.error import TimedOut, NetworkError, RetryAfter

from media_processor import download_media, clear_data_folder

load_dotenv()  # Загружаем переменные из .env

# токен Telegram-бота и ID чата
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_V = os.getenv("TELEGRAM_CHAT_V")
TELEGRAM_CHAT_PL = os.getenv("TELEGRAM_CHAT_PL")

# URL сайтов
URLS_V = os.getenv("URLS_V", "").split(",")
URLS_PL = os.getenv("URLS_PL", "").split(",")
URLS = URLS_V + URLS_PL

UNWANTED_TAGS =os.getenv("UNWANTED_TAGS", "").split(",")  # Нежелательные теги, посты с этим тегом будут пропущены

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_V:
    raise ValueError("Error: (.env) Environment variables not set!")

# Настраиваем логирование прямо в консоль (stdout) для Docker/Portainer
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger("JoyBot")
HEADERS = {"User-Agent": "Mozilla/5.0"}

# Устанавливаем таймауты (убираем ошибку timeout)
request = HTTPXRequest(connect_timeout=60, read_timeout=60)

# Ограничения Telegram
LIMIT_CAPTION = 1024  # Лимит символов описания поста телеграмм
LIMIT_TEXT_MSG = 4096  # Лимит символов для одного сообщения телеграмм
MAX_MEDIA_PER_GROUP = 10  # Лимит Telegram на медиа-группу

# Списки
SAVE_FILE = "sent_posts.pkl"# Файл данными об отправленных постах
processed_posts = defaultdict(lambda: deque(maxlen=MAX_POSTS))  # Словарь с обработанными постами (отдельно для каждого сайта) с авто удалением старых записей

MAX_POSTS = 30
recorded_sent_posts = False  # Флаг наличия несохраненных отправленных постов

#Загрузка отправленных постов sent_posts из файла SAVE_FILE
async def load_sent_posts(app: Application):
    global processed_posts
    try:
        async with aiofiles.open(SAVE_FILE, "rb") as file:
            content = await file.read()  # Асинхронно читаем файл
            if content:
                loaded_data = pickle.loads(content)  # Десериализуем
                # Преобразуем обратно в defaultdict с deque
                processed_posts = defaultdict(lambda: deque(maxlen=MAX_POSTS),
                                         {key: deque(value, maxlen=MAX_POSTS) for key, value in loaded_data.items()})
                logger.info("Sent posts data successfully loaded!")
    except FileNotFoundError:
        logger.info("File with saved posts not found, create a new one.")
    except Exception as e:
        logger.info(f"Error loading: {e}")

#Сохранение отправленных постов sent_posts в файл SAVE_FILE
async def save_sent_posts(app: Application):
    #logger.info("Saving data before exiting...")
    #print(f"Сохраняем данные: {sent_posts}")

    # Преобразуем defaultdict в обычный dict, иначе pickle не сможет его сохранить
    normal_dict = {key: list(value) for key, value in processed_posts.items()}
    try:
        async with aiofiles.open(SAVE_FILE, "wb") as file:
            await file.write(pickle.dumps(normal_dict))        
        #logger.info("Data saved successfully!")
    except Exception as e:        
        logger.info(f"Error while saving: {e}")

# Список ошибок при которых будет производиться попытка повторной отправки
def is_retryable_error(e):
    if isinstance(e, (TimedOut, NetworkError, RetryAfter)):
        return True
    text = str(e).lower()

    retry_texts = [
        "timed out",
        "timeout",
        "networkerror",
        "connecterror",
        "all connection attempts failed",
        "cannot connect to host",
        "getaddrinfo failed",
        "connection reset",
        "server disconnected",
        "retry after",
        "flood control",
        "bad gateway",
        "gateway timeout",
        "webpage_media_empty",
        "webpage_curl_failed",
    ]
    return any(t in text for t in retry_texts)


# Функция для парсинга одного поста из joy.reactor.cc
def parse_joy_post(post):
    post_data = {"content": []}
    text_content = []

    # 1. Сначала работаем с тегами (H2). Если есть стоп-теги — сразу прерываемся.
    for H2 in post.find_all('h2'):
        text = H2.get_text(", ", strip=True)
        if text:
            if any(tag in text for tag in UNWANTED_TAGS):  # Проверка поста на нежелательные теги
                return {}, []  # Завершаем функцию, пост не обрабатывается
            else:
                post_data["content"].append(
                    {"id": str(uuid.uuid4()), "type": "h2", "data": html.escape(text) + "\n", "send": "yes"})

    # 2. Изолируем и обрабатываем "Отличные комментарии" ДО того как парсить остальной текст
    best_comments_text = []
    for comment_block in post.find_all('div', class_='post_comment_list'):
        # Ищем комментарии с заливкой (это и есть "Отличные" / "Лучшие")
        best_comments = comment_block.find_all('div', class_='comment filled')

        if best_comments and not best_comments_text:
            best_comments_text.append("\n\n💬 <b>Отличные комментарии:</b>\n")

        for comment in best_comments:
            content_div = comment.find('div', class_='comment-content')
            if content_div:
                # Извлекаем никнейм автора
                author_tag = comment.find('a', class_='comment_username')
                author = html.escape(author_tag.get_text(strip=True)) if author_tag else "Аноним"

                # Ищем картинки внутри комментария
                for img_div in content_div.find_all('div', class_='image'):
                    img = img_div.find('img')
                    if img and img.get('src'):
                        img_url = img.get('src')
                        # Джойреактор часто отдает ссылки без https:, исправляем это
                        if img_url.startswith('//'):
                            img_url = "https:" + img_url

                        # Добавляем картинку из коммента в общую группу медиа поста
                        post_data["content"].append({
                            "id": str(uuid.uuid4()),
                            "type": "photo",
                            "data": img_url,
                            "send": "not"
                        })
                    # ВАЖНО: Вырезаем блок картинки из HTML комментария, чтобы он не попал в текст
                    img_div.decompose()

                # Забираем чистый текст комментария (так как картинки мы уже вырезали строкой выше)
                comment_text = content_div.get_text(strip=True)
                if comment_text:
                    best_comments_text.append(f"👤 <i>{author}</i>: {html.escape(comment_text)}\n")

        # ВАЖНО: Полностью удаляем весь блок комментариев из DOM-дерева текущего поста!
        # Теперь последующие циклы H3, P и IMAGE не найдут мусор из комментов.
        comment_block.decompose()

    # 3. Работаем с текстом (H3) основного поста
    for H3 in post.find_all('h3'):
        text = H3.get_text(strip=True)
        if text:
            text_content.append(html.escape(text) + " \n")

    # 4. Работаем с текстом (p) и ссылками внутри него a href
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

    # 5. Работаем с <div class="image"> основного поста
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

    # 6. Работаем с <iframe это coub, youtube, vimeo
    for iframe in post.find_all('iframe'):
        iframe_url = iframe.get('src')
        if iframe_url:
            parsed = urlparse(iframe_url)
            clean_path = parsed.path
            netloc = parsed.netloc

            # Удаляем параметры
            clean_url = urlunparse((parsed.scheme, netloc, clean_path, '', '', ''))

            # Преобразуем embed-ссылки
            if 'youtube.com' in netloc and '/embed/' in clean_path:
                video_id = clean_path.split('/embed/')[-1]
                clean_url = f"https://www.youtube.com/watch?v={video_id}"

            elif 'coub.com' in netloc and '/embed/' in clean_path:
                video_id = clean_path.split('/embed/')[-1]
                clean_url = f"https://coub.com/view/{video_id}"

            elif 'player.vimeo.com' in netloc and '/video/' in clean_path:
                video_id = clean_path.split('/video/')[-1]
                clean_url = f"https://vimeo.com/{video_id}"

            post_data["content"].append(
                {"id": str(uuid.uuid4()), "type": "video_hosting", "data": clean_url, "send": "not"})

    # 7. Приклеиваем текст собранных лучших комментариев в самый низ поста
    if best_comments_text:
        text_content.extend(best_comments_text)

    return post_data, text_content

# Функция для отправки текста в Telegram
async def send_text_to_telegram(context: ContextTypes.DEFAULT_TYPE, chat_id, text_content, caption):
    message = "".join(text_content) + "\n" + caption
    if message.strip():
        # Разделяем текст на части, если он превышает лимит
        parts = [message[i:i + LIMIT_TEXT_MSG] for i in range(0, len(message), LIMIT_TEXT_MSG)]

        for part in parts:
            try:
                await context.bot.send_message(chat_id=chat_id, text=part, parse_mode=ParseMode.HTML)
            except Exception as e:
                print(f"Text error: {e}")
                await context.bot.send_message(chat_id=chat_id, text=html.escape(part), parse_mode=ParseMode.HTML)
            await asyncio.sleep(30)  # задержка в 30 секунд чтобы не срабатывал Flood control exceeded


# Функция для отправки медиа-группы в Telegram
async def send_post(context: ContextTypes.DEFAULT_TYPE, chat_id, post_id, contents, text_content):
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

            if content["type"] == "photo":  # Если контент картинка
                match content["send"]:
                    case "not":
                        photo_group.append(
                            InputMediaPhoto(media=content["data"],
                                            caption=(caption if not photo_group else None),
                                            parse_mode="HTML"))
                        id_photo.append(content["id"])
                    case "err":
                        # Качаем файл если по ссылке не отправляется
                        downloaded_files = await download_media(content["data"])
                        if downloaded_files:
                            # Проходим циклом, так как media_processor возвращает список
                            for file_path in downloaded_files:
                                with open(file_path, 'rb') as file:
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
                        downloaded_files = await download_media(content["data"])
                        if downloaded_files:
                            for file_path in downloaded_files:
                                video_group.append(InputMediaVideo(
                                    media=open(file_path, 'rb'),
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
                        downloaded_files = await download_media(content["data"])
                        if downloaded_files:
                            for file_path in downloaded_files:
                                gif_group.append(InputMediaAnimation(
                                    media=open(file_path, 'rb'),
                                    caption=(caption if not gif_group else None),
                                    parse_mode="HTML"
                                ))
                        else:
                            content["send"] = "close"
                        id_gif.append(content["id"])
                    case "err":  # Пробуем качать видео на диск
                        downloaded_file = await download_media(content["data"])
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

                    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
                    content["send"] = "yes"
            else:
                await context.bot.send_message(chat_id=chat_id, text=f'Не известный контент {link_post} {content["data"]}')
                content["send"] = "close"

            if photo_group and (
                    (len(photo_group) == MAX_MEDIA_PER_GROUP) or (
                    len(photo_group) >= (type_counts.get('photo', 0) - count_send_photo)) or (
                            index == len(content_list) - 1)):
                try:
                    await context.bot.send_media_group(chat_id=chat_id, media=photo_group)
                    logger.info(f"Post {post_id} - Send pict {len(photo_group)}: {photo_group}")
                    for item in content_list:
                        if item["id"] in id_photo:  # Проверяем, есть ли ID в списке
                            item["send"] = "yes"
                            count_send_photo += 1
                except Exception as e:
                    retry = is_retryable_error(e)
                    status_text = "идет на повтор" if retry else "пропущен (закрыт)"
                    logger.error(f"Error in post {post_id}: {type(e).__name__} ({e}). Статус: {status_text}")

                    for item in content_list:
                        if item["id"] in id_photo:
                            if item["send"] == "not":
                                item["send"] = "err"  # При первой ошибке пробуем скачать локально
                            elif retry:
                                # Ошибка сети при отправке файла: оставляем статус 'err',
                                # чтобы на следующем круге цикла попытаться снова.
                                pass
                            else:
                                item["send"] = "close"  # Фатальная ошибка, закрываем
                                count_send_photo += 1

                    if retry:
                        # Если сеть отвалилась, делаем паузу перед повторным кругом цикла,
                        # чтобы не уйти в бесконечный моментальный спам запросами
                        await asyncio.sleep(10)
                photo_group.clear()
                id_photo.clear()

            if video_group and (
                    (len(video_group) == MAX_MEDIA_PER_GROUP) or (
                    len(video_group) == type_counts.get('video', 0) - count_send_video) or (
                            index == len(content_list) - 1)):
                try:
                    await context.bot.send_media_group(chat_id=chat_id, media=video_group)
                    logger.info("Post %s - Send video %s",post_id, video_group)
                    for item in content_list:
                        if item["id"] in id_video:  # Проверяем, есть ли ID в списке
                            item["send"] = "yes"
                            count_send_video += 1
                except Exception as e:
                    print(f"Error in post {post_id}: {e}")
                    # not_processed = True
                    for item in content_list:
                        if item["id"] in id_video:  # Проверяем, есть ли ID в списке
                            if item["send"] == "not":
                                item["send"] = "err"
                            else:
                                item["send"] = "close"
                                count_send_video += 1                
                video_group.clear()
                id_video.clear()

            if gif_group and ((len(gif_group) == MAX_MEDIA_PER_GROUP) or (
                    len(gif_group) == type_counts.get('gif', 0) - count_send_gif) or (index == len(content_list) - 1)):
                try:
                    for gif_file in gif_group:
                        await context.bot.send_animation(chat_id=chat_id, animation=gif_file.media, caption=caption,
                                                 parse_mode="HTML")
                        logger.info("Post %s - Send gif %s",post_id, gif_group)
                    for item in content_list:
                        if item["id"] in id_gif:  # Проверяем, есть ли ID в списке
                            item["send"] = "yes"
                            count_send_gif += 1
                except Exception as e:
                    print(f"Error in post {post_id}: {e}")
                    # not_processed = True
                    for item in content_list:
                        if item["id"] in id_gif:  # Проверяем, есть ли ID в списке
                            if item["send"] == "not":
                                item["send"] = "err"
                            else:
                                item["send"] = "close"
                                count_send_gif += 1                
                gif_group.clear()
                id_gif.clear()
        if text_content:
            await send_text_to_telegram(context,chat_id, text_content, caption)  # Отправляем длинные тексты
            text_content.clear()
    if not everything_sent:
        await context.bot.send_message(chat_id=chat_id,
                               text=link_post + "Не все удалось отправить, чтобы посмотреть нажмите на пост",
                               parse_mode=ParseMode.HTML)

    await clear_data_folder()  # Удаляем скачанные файлы

async def fetch_html(url):
    """Асинхронный запрос к сайту."""
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(url) as response:
                if response.status == 200:
                    return await response.text()
                else:
                    logger.error(f"Site loading error: {response.status}")
                    return "" #[]
    except Exception as e: #если нет доступа до сайта
        logger.error(f"Connection error to {url}: {e}")
        return ""


# Основной цикл для проверки новых постов
async def monitor_website(context: ContextTypes.DEFAULT_TYPE):
    global recorded_sent_posts
    post_id = 0
    has_new_posts = False  # Флаг, отслеживающий появление постов именно в этот проход
    try:
        for url in URLS:
            html_text = await fetch_html(url)
            if not html_text: continue

            chat_id = TELEGRAM_CHAT_V if url in URLS_V else TELEGRAM_CHAT_PL
            soup = BeautifulSoup(html_text, "html.parser")
            posts = soup.find_all("div", class_="postContainer")
            for post in posts:
                post_id_full = post.get("id")  # Уникальный идентификатор поста
                post_id = post_id_full.split('postContainer')[-1].strip('"')
                if post_id not in processed_posts[url]:
                    post_data, text_content = parse_joy_post(post)
                    # Отправляем данные с поста
                    if post_data:
                        await send_post(context,chat_id=chat_id, post_id=post_id, contents=post_data,
                                        text_content=text_content)

                    processed_posts[url].append(post_id)  # помечаем что пост отправлен

                    # Включаем флаги сохранения
                    has_new_posts = True
                    recorded_sent_posts = True

                    # Небольшая пауза между постами, чтобы не спамить
                    await asyncio.sleep(5)
        # Если цикл завершился, новых постов не было, НО висят несохраненные данные с прошлых раз
        if not has_new_posts and recorded_sent_posts:
            await save_sent_posts(context.application)
            recorded_sent_posts = False

    except Exception as e:
        print(f"Error in post {post_id}: {e}")

# Запуск программы
if __name__ == "__main__":
    #asyncio.run(main())  # Запуск главной асинхронной функции
    # 1. Настраиваем RateLimiter (Решает проблему Flood Control)
    rate_limiter = AIORateLimiter(
        overall_max_rate=30,  # Не более 30 сообщений в секунду (общий лимит)
        overall_time_period=1,
        group_time_period=60,  # Групповые лимиты
        max_retries=5  # Сколько раз повторять при ошибке сети
    )

    # 2. Создаем приложение
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(request)
        .rate_limiter(rate_limiter)  # Подключаем лимитер
        .post_init(load_sent_posts)  # Загрузка данных при старте
        .post_shutdown(save_sent_posts)  # Сохранение данных при выходе (SIGTERM)
        .build()
    )

    # 3. Добавляем задачу мониторинга в очередь
    # run_repeating запускает monitor_website_job каждые 60 секунд
    # first=1 означает первый запуск через 1 секунду после старта
    application.job_queue.run_repeating(monitor_website, interval=60, first=1)

    # 4. Запускаем бота (Polling)
    # Это блокирующая операция, которая сама обрабатывает сигналы systemd
    logger.info("Bot started via PTB Application")
    # Запуск с запросом на удаление вебхука и сброс накопившейся очереди
    application.run_polling(drop_pending_updates=True)

