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
from telegram import InputMediaPhoto, InputMediaVideo, InputMediaAnimation, LinkPreviewOptions
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
    comment_media = []

    # 1. Работаем с тегами (H2)
    for H2 in post.find_all('h2'):
        raw_text = H2.get_text(", ", strip=True)
        if raw_text:
            if any(tag in raw_text for tag in UNWANTED_TAGS):
                return {}, []
            else:
                tags = [t.strip() for t in raw_text.split(",")]
                formatted_tags_list = [f"<code>{html.escape(t)}</code>" for t in tags if t]
                formatted_tags = ", ".join(formatted_tags_list)
                post_data["content"].append(
                    {"id": str(uuid.uuid4()), "type": "h2", "data": formatted_tags, "send": "yes"})

    # 2. Изолируем и обрабатываем "Отличные комментарии"
    best_comments_text = []
    filled_comments = post.find_all('div', id=lambda x: x and x.startswith('best_comment_'))

    if filled_comments:
        logger.info(f"Парсинг: Найдено отличных комментариев - {len(filled_comments)}")
        best_comments_text.append("\n\n💬 <b>Отличные комментарии:</b>\n")

        for comment in filled_comments:
            content_div = comment.find('div', class_='comment-content')
            if not content_div: continue

            if content_div.find('div', class_='txt') and "Нажмите, чтобы показать" in content_div.get_text():
                continue

            author_tag = comment.find('a', class_='comment_username')
            author = html.escape(author_tag.get_text(strip=True)) if author_tag else "Аноним"
            has_media = False

            for iframe in content_div.find_all('iframe'):
                iframe_url = iframe.get('src')
                if iframe_url:
                    parsed = urlparse(iframe_url)
                    clean_url = f"https://{parsed.netloc}{parsed.path}"
                    if 'youtube.com' in clean_url:
                        clean_url = clean_url.replace('/embed/', '/watch?v=')
                    elif 'coub.com' in clean_url:
                        clean_url = clean_url.replace('/embed/', '/view/')
                    elif 'vimeo.com' in clean_url:
                        clean_url = clean_url.replace('/video/', '/')
                    comment_media.append(
                        {"id": str(uuid.uuid4()), "type": "video_hosting", "data": clean_url, "send": "not"})
                    has_media = True
                iframe.decompose()

            for img_div in content_div.find_all('div', class_='image'):
                img_tag = ""
                for video_span in img_div.find_all('span', class_='video_holder'):
                    img_tag = "video"
                    source = video_span.find('source', type="video/mp4")
                    if source: comment_media.append(
                        {"id": str(uuid.uuid4()), "type": "video", "data": "https:" + source.get('src'), "send": "not"})

                for gif_span in img_div.find_all('span', class_='video_gif_holder'):
                    img_tag = "gif"
                    source = gif_span.find('source', type="video/mp4")
                    if source:
                        comment_media.append(
                            {"id": str(uuid.uuid4()), "type": "video", "data": "https:" + source.get('src'),
                             "send": "not"})
                    else:
                        a_tag = gif_span.find('a')
                        if a_tag: comment_media.append(
                            {"id": str(uuid.uuid4()), "type": "gif", "data": "https:" + a_tag.get('href'),
                             "send": "not"})

                for full_div in img_div.find_all('a', class_='prettyPhotoLink'):
                    img_tag = "photo"
                    comment_media.append(
                        {"id": str(uuid.uuid4()), "type": "photo", "data": "https:" + full_div.get('href'),
                         "send": "not"})

                if not img_tag:
                    img = img_div.find('img')
                    if img and img.get('src'):
                        url = img['src']
                        if url.startswith('//'): url = "https:" + url
                        comment_media.append({"id": str(uuid.uuid4()), "type": "photo", "data": url, "send": "not"})

                has_media = True
                img_div.decompose()  # Удаляем картинку из DOM комментария

            comment_text = content_div.get_text(separator=" ", strip=True)
            if comment_text:
                best_comments_text.append(f"👤 <i>{author}</i>: {html.escape(comment_text)}\n")
            elif has_media:
                best_comments_text.append(f"👤 <i>{author}</i>: [Прикрепленный медиафайл]\n")

    # Уничтожаем блоки комментариев в оригинальном HTML
    for comment_list in post.find_all('div', class_='post_comment_list'):
        comment_list.decompose()

    # 3. Извлекаем медиа основного поста и УДАЛЯЕМ их теги

    # СНАЧАЛА ищем iframe, так как они лежат внутри div.image и могут удалиться раньше времени!
    for iframe in post.find_all('iframe'):
        iframe_url = iframe.get('src')
        if iframe_url:
            parsed = urlparse(iframe_url)
            clean_url = f"https://{parsed.netloc}{parsed.path}"
            if 'youtube.com' in clean_url:
                clean_url = clean_url.replace('/embed/', '/watch?v=')
            elif 'coub.com' in clean_url:
                clean_url = clean_url.replace('/embed/', '/view/')
            elif 'vimeo.com' in clean_url:
                clean_url = clean_url.replace('/video/', '/')
            post_data["content"].append(
                {"id": str(uuid.uuid4()), "type": "video_hosting", "data": clean_url, "send": "not"})

        iframe.decompose()

    # ПОТОМ обрабатываем div.image (теперь оттуда безопасно удалять элементы)
    for img_div in post.find_all('div', class_='image'):
        img_tag = ""
        for video_span in img_div.find_all('span', class_='video_holder'):
            img_tag = "video"
            source = video_span.find('source', type="video/mp4")
            if source: post_data["content"].append(
                {"id": str(uuid.uuid4()), "type": "video", "data": "https:" + source.get('src'), "send": "not"})

        for gif_span in img_div.find_all('span', class_='video_gif_holder'):
            img_tag = "gif"
            source = gif_span.find('source', type="video/mp4")
            if source:
                post_data["content"].append(
                    {"id": str(uuid.uuid4()), "type": "video", "data": "https:" + source.get('src'), "send": "not"})
            else:
                a_tag = gif_span.find('a')
                if a_tag: post_data["content"].append(
                    {"id": str(uuid.uuid4()), "type": "gif", "data": "https:" + a_tag.get('href'), "send": "not"})

        for full_div in img_div.find_all('a', class_='prettyPhotoLink'):
            img_tag = "photo"
            post_data["content"].append(
                {"id": str(uuid.uuid4()), "type": "photo", "data": "https:" + full_div.get('href'), "send": "not"})

        if not img_tag:
            img = img_div.find('img')
            if img and img.get('src'):
                url = img['src']
                if url.startswith('//'): url = "https:" + url
                post_data["content"].append({"id": str(uuid.uuid4()), "type": "photo", "data": url, "send": "not"})

        # ВАЖНО: Удаляем блок картинки из HTML.
        img_div.decompose()

    # 4. Собираем чистый текст основного поста (теперь он без мусора от медиа)
    post_content_div = post.find('div', class_='post_content')
    if post_content_div:
        inner_div = post_content_div.find('div')
        if inner_div:
            parts = []
            # Перебираем ВСЕ дочерние элементы (и <p>, и <b>, и просто текст)
            for element in inner_div.children:
                if isinstance(element, str):
                    text = element.strip()
                    if text: parts.append(html.escape(text))
                elif element.name == 'a':
                    href = element.get("href")
                    link_text = element.get_text(strip=True)
                    if link_text and href:
                        if len(link_text) > 30: link_text = link_text[:27] + "..."
                        parts.append(f'<a href="{html.escape(href)}">{html.escape(link_text)}</a>')
                elif element.name == 'p':
                    # Если текст все же обернут в <p>
                    for p_el in element.contents:
                        if isinstance(p_el, str):
                            t = p_el.strip()
                            if t: parts.append(html.escape(t))
                        elif p_el.name == 'a':
                            href = p_el.get("href")
                            link_text = p_el.get_text(strip=True)
                            if link_text and href:
                                if len(link_text) > 30: link_text = link_text[:27] + "..."
                                parts.append(f'<a href="{html.escape(href)}">{html.escape(link_text)}</a>')
                        else:
                            t = p_el.get_text(strip=True)
                            if t: parts.append(html.escape(t))
                else:
                    # Ловит <b>, <h3>, <span> и прочие теги, которые лепят авторы
                    t = element.get_text(strip=True)
                    if t: parts.append(html.escape(t))

            if parts:
                text_content.append(" ".join(parts) + "\n")

    # 5. Приклеиваем текст собранных лучших комментариев
    if best_comments_text:
        text_content.extend(best_comments_text)

    # 6. ВАЖНО: Добавляем медиа из комментариев в самый конец
    if comment_media:
        post_data["content"].extend(comment_media)

    return post_data, text_content

# Функция для отправки текста в Telegram
async def send_text_to_telegram(context: ContextTypes.DEFAULT_TYPE, chat_id, text_content, caption):
    message = "".join(text_content) + "\n" + caption
    if message.strip():
        # Разделяем текст на части, если он превышает лимит
        parts = [message[i:i + LIMIT_TEXT_MSG] for i in range(0, len(message), LIMIT_TEXT_MSG)]

        for part in parts:
            if not part.strip():
                continue # Защита от пустых кусков
            try:
                await context.bot.send_message(chat_id=chat_id, text=part, parse_mode=ParseMode.HTML)
            except Exception as e:
                print(f"Text error: {e}")
                try:
                    await context.bot.send_message(chat_id=chat_id, text=html.escape(part), parse_mode=ParseMode.HTML)
                except Exception as inner_e:
                    print(f"Failed to send text fallback: {inner_e}")
            await asyncio.sleep(30)


# --- Разделяем медиа на группы MAX_MEDIA_PER_GROUP ---
async def _process_media_group(context: ContextTypes.DEFAULT_TYPE, chat_id, post_id, media_objects, related_items,
                               caption=None):
    """
    Разбивает медиагруппу на чанки по 10 штук и безопасно отправляет.
    Если передан caption, он добавляется только к самому последнему чанку в группе.
    Возвращает True, если caption был успешно отправлен, иначе False.
    """
    caption_sent_here = False

    while media_objects:
        chunk = media_objects[:MAX_MEDIA_PER_GROUP]
        chunk_items = related_items[:MAX_MEDIA_PER_GROUP]

        is_last_chunk = len(media_objects) <= MAX_MEDIA_PER_GROUP

        # Если это последний чанк и нам выдали caption — прикрепляем его к первому файлу чанка
        if is_last_chunk and caption:
            first_media = chunk[0]
            if isinstance(first_media, InputMediaPhoto):
                chunk[0] = InputMediaPhoto(media=first_media.media, caption=caption, parse_mode="HTML")
            elif isinstance(first_media, InputMediaVideo):
                chunk[0] = InputMediaVideo(media=first_media.media, caption=caption, parse_mode="HTML")

        try:
            await context.bot.send_media_group(chat_id=chat_id, media=chunk)
            #logger.info(f"Post {post_id} - Sent media chunk ({len(chunk)} items)")

            if is_last_chunk and caption:
                caption_sent_here = True

            # Помечаем оригинальные элементы словаря как отправленные
            for item in chunk_items:
                item["send"] = "yes"

            # Убираем успешный чанк из очереди
            media_objects = media_objects[MAX_MEDIA_PER_GROUP:]
            related_items = related_items[MAX_MEDIA_PER_GROUP:]

            if media_objects:
                await asyncio.sleep(3)  # Небольшая пауза между кусками большого альбома

        except Exception as e:
            retry = is_retryable_error(e)
            status_text = "идет на повтор" if retry else "пропущен (закрыт)"
            logger.error(f"Error in post {post_id}: {type(e).__name__} ({e}). Статус: {status_text}")

            for item in chunk_items:
                if item["send"] == "not":
                    item["send"] = "err"
                elif not retry:
                    item["send"] = "close"

            if retry:
                await asyncio.sleep(10)

            break  # При ошибке прерываем цикл чанков, остатки подхватит внешний цикл

    return caption_sent_here


# --- Основная функция отправки постов ---
async def send_post(context: ContextTypes.DEFAULT_TYPE, chat_id, post_id, contents, text_content):
    content_list = contents.get("content", [])
    title = next((item["data"] for item in content_list if item["type"] == "h2"), "")
    link_post = f'<a href="https://m.joyreactor.cc/post/{post_id}">Пост {post_id}</a>'

    # Формируем базовое описание
    base_caption = title + "\n" + link_post
    post_caption = base_caption

    # Проверяем, есть ли в посте вообще хоть какое-то медиа
    has_media = any(item["type"] in ["photo", "video", "gif", "video_hosting"] for item in content_list)

    # 1. РАБОТАЕМ С ТЕКСТОМ (Отправляем длинный текст ДО медиа)
    if text_content:
        if has_media:
            # Если текст влезет в лимит подписи - объединяем, он уйдет в самом конце альбома
            if (sum(len(text) for text in text_content) + len(base_caption + "\n")) < LIMIT_CAPTION:
                post_caption = base_caption + "\n" + "".join(text_content)
                text_content.clear()
            else:
                # Текст не влезает! Отправляем его СЕЙЧАС отдельным сообщением (без подписи)
                await send_text_to_telegram(context, chat_id, text_content, "")
                text_content.clear()
                # post_caption остается просто базовым (ссылка + заголовок)
        else:
            # Медиа нет вообще (только текст). Мы его пока не трогаем, он улетит в самом низу функции вместе с подписью
            pass

    caption_sent = not has_media  # Если медиа нет, считать что мы "закончили" с прикреплением
    not_processed = True
    everything_sent = True

    # 2. ОСНОВНОЙ ЦИКЛ ОБРАБОТКИ МЕДИА
    while not_processed:
        not_processed = False
        download_retry_occurred = False  # Флаг для паузы при ошибках скачивания

        photo_group, photo_items = [], []
        video_group, video_items = [], []
        gif_group, gif_items = [], []
        video_hosting_group = []

        # СОБИРАЕМ МЕДИА-КОНТЕНТ (без caption!)
        for content in content_list:
            if content["send"] == "yes":
                continue
            elif content["send"] == "close":
                everything_sent = False
                continue
            else:
                not_processed = True

            media_type = content["type"]

            if media_type == "photo":
                if content["send"] == "not":
                    photo_group.append(InputMediaPhoto(media=content["data"], parse_mode="HTML"))
                    photo_items.append(content)
                elif content["send"] == "err":
                    files = await download_media(content["data"])
                    if files:
                        for f_path in files:
                            with open(f_path, 'rb') as file:
                                photo_group.append(InputMediaPhoto(media=file.read(), parse_mode="HTML"))
                                photo_items.append(content)
                    else:
                        # Возвращаем утерянную логику попыток скачивания!
                        retries = content.get("retries", 0) + 1
                        content["retries"] = retries
                        if retries <= 3:
                            download_retry_occurred = True
                            logger.info(f"Retrying download for {content['data']} (Attempt {retries}/3)")
                        else:
                            content["send"] = "close"

            elif media_type == "video":
                if content["send"] == "not":
                    video_group.append(InputMediaVideo(media=content["data"], parse_mode="HTML"))
                    video_items.append(content)
                elif content["send"] == "err":
                    files = await download_media(content["data"])
                    if files:
                        for f_path in files:
                            video_group.append(InputMediaVideo(media=open(f_path, 'rb'), parse_mode="HTML"))
                            video_items.append(content)
                    else:
                        retries = content.get("retries", 0) + 1
                        content["retries"] = retries
                        if retries <= 3:
                            download_retry_occurred = True
                            logger.info(f"Retrying download for {content['data']} (Attempt {retries}/3)")
                        else:
                            content["send"] = "close"

            elif media_type == "gif":
                if content["send"] == "not":
                    gif_group.append(InputMediaAnimation(media=content["data"], parse_mode="HTML"))
                    gif_items.append(content)
                elif content["send"] == "err":
                    files = await download_media(content["data"])
                    if files:
                        for f_path in files:
                            gif_group.append(InputMediaAnimation(media=open(f_path, 'rb'), parse_mode="HTML"))
                            gif_items.append(content)
                    else:
                        retries = content.get("retries", 0) + 1
                        content["retries"] = retries
                        if retries <= 3:
                            download_retry_occurred = True
                            logger.info(f"Retrying download for {content['data']} (Attempt {retries}/3)")
                        else:
                            content["send"] = "close"

            elif media_type == "video_hosting" and content["send"] in ["not", "err"]:
                video_hosting_group.append(content)

        # --- ОТПРАВЛЯЕМ МЕДИА-КОНТЕНТ ---

        # Вычисляем, какая группа будет отправляться самой последней в этой итерации
        last_media_type = None
        if video_hosting_group:
            last_media_type = "video_hosting"
        elif gif_group:
            last_media_type = "gif"
        elif video_group:
            last_media_type = "video"
        elif photo_group:
            last_media_type = "photo"

        if photo_group:
            sent = await _process_media_group(
                context, chat_id, post_id, photo_group, photo_items,
                caption=post_caption if last_media_type == "photo" and not caption_sent else None
            )
            if sent: caption_sent = True

        if video_group:
            sent = await _process_media_group(
                context, chat_id, post_id, video_group, video_items,
                caption=post_caption if last_media_type == "video" and not caption_sent else None
            )
            if sent: caption_sent = True

        if gif_group:
            for i, (gif, item) in enumerate(zip(gif_group, gif_items)):
                is_last_gif = (i == len(gif_group) - 1)
                current_caption = post_caption if is_last_gif and last_media_type == "gif" and not caption_sent else None

                try:
                    await context.bot.send_animation(chat_id=chat_id, animation=gif.media, caption=current_caption,
                                                     parse_mode="HTML")
                    logger.info(f"Post {post_id} - Sent gif")
                    item["send"] = "yes"
                    if current_caption:
                        caption_sent = True
                except Exception as e:
                    retry = is_retryable_error(e)
                    logger.error(f"Error in post {post_id} (gif): {e}")
                    if item["send"] == "not":
                        item["send"] = "err"
                    elif not retry:
                        item["send"] = "close"
                    if retry:
                        await asyncio.sleep(10)

        if video_hosting_group:
            for i, item in enumerate(video_hosting_group):
                is_last_vh = (i == len(video_hosting_group) - 1)
                current_caption = post_caption if is_last_vh and last_media_type == "video_hosting" and not caption_sent else None

                text = f'<a href="{item["data"]}">📺 Смотреть видео</a>'
                if current_caption:
                    text += f'\n\n{current_caption}'

                try:
                    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                                                   link_preview_options=LinkPreviewOptions(is_disabled=False))
                    item["send"] = "yes"
                    if current_caption:
                        caption_sent = True

                except Exception as e:
                    if "webpage" in str(e).lower():
                        logger.warning(f"Превью не сгенерировалось для {item['data']}, отправляем без него.")
                        try:
                            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                                                           link_preview_options=LinkPreviewOptions(is_disabled=True))
                            item["send"] = "yes"
                            if current_caption:
                                caption_sent = True
                        except Exception as inner_e:
                            logger.error(f"Fallback error: {inner_e}")
                            item["send"] = "err"
                    else:
                        retry = is_retryable_error(e)
                        if item["send"] == "not":
                            item["send"] = "err"
                        elif not retry:
                            item["send"] = "close"
                        if retry:
                            await asyncio.sleep(10)

        # Если были проблемы со скачиванием, ждем 5 секунд перед следующим кругом
        if download_retry_occurred:
            await asyncio.sleep(5)

    # 3. ОТПРАВЛЯЕМ ОСТАТКИ ТЕКСТА (Для постов, где вообще не было картинок)
    if text_content:
        await send_text_to_telegram(context, chat_id, text_content, base_caption)
        text_content.clear()

    if not everything_sent:
        await context.bot.send_message(chat_id=chat_id,
                                       text=link_post + "Не все удалось отправить, чтобы посмотреть нажмите на пост",
                                       parse_mode=ParseMode.HTML)

    await clear_data_folder()

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

