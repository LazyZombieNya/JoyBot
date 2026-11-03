import asyncio
import html
import logging
import os
import pickle
import sys
import uuid
from collections import deque, defaultdict
from urllib.parse import urlparse

import aiofiles
import aiohttp
import httpx
from bs4 import BeautifulSoup, NavigableString, Tag
from dotenv import load_dotenv
from telegram import InputMediaPhoto, InputMediaVideo, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.ext import Application, ApplicationBuilder, ContextTypes, AIORateLimiter
from telegram.request import HTTPXRequest
from telegram.error import TimedOut, NetworkError, RetryAfter

from media_processor import download_media, clear_data_folder

load_dotenv()

# Токен Telegram-бота и ID чатов
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_V = os.getenv("TELEGRAM_CHAT_V")
TELEGRAM_CHAT_PL = os.getenv("TELEGRAM_CHAT_PL")

# URL сайтов
URLS_V = [u.strip() for u in os.getenv("URLS_V", "").split(",") if u.strip()]
URLS_PL = [u.strip() for u in os.getenv("URLS_PL", "").split(",") if u.strip()]
URLS = URLS_V + URLS_PL

UNWANTED_TAGS = [t.strip() for t in os.getenv("UNWANTED_TAGS", "").split(",") if t.strip()]

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_V:
    raise ValueError("Error: (.env) Environment variables not set!")

# Логирование в stdout для Docker/Portainer
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger("JoyBot")
HEADERS = {"User-Agent": "Mozilla/5.0"}

# Увеличенные таймауты для стабильной выгрузки тяжелых файлов
request = HTTPXRequest(
    connect_timeout=60,
    read_timeout=120,
    write_timeout=120,
    pool_timeout=60
)

# Лимиты Telegram и Rich Messages (Bot API 10.1-10.3)
MAX_RICH_MEDIA = 50           # До 50 медиавложений в одном Rich Message
LIMIT_RICH_CHARS = 31000      # Безопасный лимит текста в Rich Message (максимум API: 32 768)
PREVIEW_CHARS_LIMIT = 600     # Сколько символов текста оставлять открытыми до спойлера "Читать далее"
LIMIT_CAPTION = 1024          # Лимит обычной подписи к медиа
LIMIT_TEXT_MSG = 4000         # Безопасный лимит одного обычного сообщения при фолбэке
MAX_MEDIA_PER_GROUP = 10      # Лимит обычной медиагруппы (для видео/фолбэка)
MAX_SEND_RETRIES = 2          # Лимит попыток при сетевых сбоях

# Хранилище отправленных постов
SAVE_FILE = "sent_posts.pkl"
MAX_POSTS = 50
processed_posts = defaultdict(lambda: deque(maxlen=MAX_POSTS))
recorded_sent_posts = False


async def load_sent_posts(app: Application):
    global processed_posts
    try:
        async with aiofiles.open(SAVE_FILE, "rb") as file:
            content = await file.read()
            if content:
                loaded_data = pickle.loads(content)
                processed_posts = defaultdict(
                    lambda: deque(maxlen=MAX_POSTS),
                    {key: deque(value, maxlen=MAX_POSTS) for key, value in loaded_data.items()}
                )
                logger.info("Sent posts data successfully loaded!")
    except FileNotFoundError:
        logger.info("File with saved posts not found, will create a new one.")
    except Exception as e:
        logger.error(f"Error loading sent posts: {e}")


async def save_sent_posts(app: Application):
    normal_dict = {key: list(value) for key, value in processed_posts.items()}
    try:
        async with aiofiles.open(SAVE_FILE, "wb") as file:
            await file.write(pickle.dumps(normal_dict))
    except Exception as e:
        logger.error(f"Error while saving sent posts: {e}")


def is_url_fetch_error(e: Exception) -> bool:
    text = str(e).lower()
    url_errors = [
        "webpage_media_empty",
        "webpage_curl_failed",
        "wrong file identifier/http url specified",
        "failed to get http url content",
    ]
    return any(t in text for t in url_errors)


def is_retryable_network_error(e: Exception) -> bool:
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
        "internal server error",
    ]
    return any(t in text for t in retry_texts)


def extract_media_from_image_div(img_div: Tag) -> list[dict]:
    """Извлекает все медиа из блока div.image."""
    found = []
    img_tag = ""

    for video_span in img_div.find_all('span', class_='video_holder'):
        img_tag = "video"
        source = video_span.find('source', type="video/mp4")
        if source and source.get('src'):
            url = source.get('src')
            if url.startswith('//'):
                url = "https:" + url
            found.append({"id": str(uuid.uuid4()), "type": "video", "data": url, "send": "not"})

    for gif_span in img_div.find_all('span', class_='video_gif_holder'):
        img_tag = "gif"
        source = gif_span.find('source', type="video/mp4")
        if source and source.get('src'):
            url = source.get('src')
            if url.startswith('//'):
                url = "https:" + url
            found.append({"id": str(uuid.uuid4()), "type": "video", "data": url, "send": "not"})
        else:
            a_tag = gif_span.find('a')
            if a_tag and a_tag.get('href'):
                url = a_tag.get('href')
                if url.startswith('//'):
                    url = "https:" + url
                found.append({"id": str(uuid.uuid4()), "type": "gif", "data": url, "send": "not"})

    for full_div in img_div.find_all('a', class_='prettyPhotoLink'):
        img_tag = "photo"
        if full_div.get('href'):
            url = full_div.get('href')
            if url.startswith('//'):
                url = "https:" + url
            found.append({"id": str(uuid.uuid4()), "type": "photo", "data": url, "send": "not"})

    if not img_tag:
        img = img_div.find('img')
        if img and img.get('src'):
            url = img['src']
            if url.startswith('//'):
                url = "https:" + url
            found.append({"id": str(uuid.uuid4()), "type": "photo", "data": url, "send": "not"})

    return found


def parse_joy_post(post: Tag) -> dict | None:
    """
    Парсит пост JoyReactor:
    - Извлекает теги (без лишних эмодзи)
    - Собирает текстовые абзацы отдельно (списком), чтобы они не слипались
    - Собирает отличные комментарии списком (каждый отдельно)
    - Собирает все медиа (основного поста + отличных комментариев)
    """
    post_data = {
        "tags_html": "",
        "paragraphs": [],      # Список HTML-строк абзацев основного текста
        "comments": [],        # Список HTML-строк отличных комментариев
        "content": []          # Список словарей медиафайлов
    }
    comment_media = []

    # 1. Теги (H2) — без эмодзи 🏷
    tags_accum = []
    for h2 in post.find_all('h2'):
        raw_text = h2.get_text(", ", strip=True)
        if raw_text:
            if any(tag in raw_text for tag in UNWANTED_TAGS):
                return None
            tags = [t.strip() for t in raw_text.split(",") if t.strip()]
            tags_accum.extend([f"<code>#{html.escape(t)}</code>" for t in tags])
    if tags_accum:
        post_data["tags_html"] = ", ".join(tags_accum)

    # 2. Отличные комментарии (каждый комментарий — отдельный элемент списка!)
    filled_comments = post.find_all('div', id=lambda x: x and x.startswith('best_comment_'))
    if filled_comments:
        logger.info(f"Парсинг: Найдено отличных комментариев - {len(filled_comments)}")
        for comment in filled_comments:
            content_div = comment.find('div', class_='comment-content')
            if not content_div:
                continue

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
                        {"id": str(uuid.uuid4()), "type": "video_hosting", "data": clean_url, "send": "not"}
                    )
                    has_media = True
                iframe.decompose()

            for img_div in content_div.find_all('div', class_='image'):
                extracted = extract_media_from_image_div(img_div)
                if extracted:
                    comment_media.extend(extracted)
                    has_media = True
                img_div.decompose()

            comment_text = content_div.get_text(separator=" ", strip=True)
            if comment_text:
                post_data["comments"].append(f"👤 <b>{author}</b>: {html.escape(comment_text)}")
            elif has_media:
                post_data["comments"].append(f"👤 <b>{author}</b>: <i>[Прикрепленный медиафайл]</i>")

    # Удаляем дерево комментариев из DOM
    for comment_list in post.find_all('div', class_='post_comment_list'):
        comment_list.decompose()

    # 3. Извлекаем iframe и медиа основного поста
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
                {"id": str(uuid.uuid4()), "type": "video_hosting", "data": clean_url, "send": "not"}
            )
        iframe.decompose()

    for img_div in post.find_all('div', class_='image'):
        extracted = extract_media_from_image_div(img_div)
        if extracted:
            post_data["content"].extend(extracted)
        img_div.decompose()

    # 4. Собираем чистый текст основного поста по абзацам
    post_content_div = post.find('div', class_='post_content')
    if post_content_div:
        inner_div = post_content_div.find('div') or post_content_div

        # Заменяем все <br> на переносы строк, чтобы текст внутри одного блока не склеивался
        for br in inner_div.find_all('br'):
            br.replace_with("\n")

        for element in inner_div.children:
            if isinstance(element, NavigableString):
                text = str(element).strip()
                if text:
                    for line in text.split("\n"):
                        if line.strip():
                            post_data["paragraphs"].append(html.escape(line.strip()))
            elif isinstance(element, Tag):
                if element.name == 'a':
                    href = element.get("href")
                    link_text = element.get_text(strip=True)
                    if link_text and href:
                        if len(link_text) > 35:
                            link_text = link_text[:32] + "..."
                        post_data["paragraphs"].append(f'<a href="{html.escape(href)}">{html.escape(link_text)}</a>')
                elif element.name in ['p', 'div']:
                    p_parts = []
                    for p_el in element.contents:
                        if isinstance(p_el, NavigableString):
                            t = str(p_el).strip()
                            if t:
                                p_parts.append(html.escape(t))
                        elif isinstance(p_el, Tag):
                            if p_el.name == 'a':
                                href = p_el.get("href")
                                link_text = p_el.get_text(strip=True)
                                if link_text and href:
                                    if len(link_text) > 35:
                                        link_text = link_text[:32] + "..."
                                    p_parts.append(f'<a href="{html.escape(href)}">{html.escape(link_text)}</a>')
                            else:
                                t = p_el.get_text(" ", strip=True)
                                if t:
                                    p_parts.append(html.escape(t))
                    joined_p = " ".join(p_parts).strip()
                    if joined_p:
                        for sub_line in joined_p.split("\n"):
                            if sub_line.strip():
                                post_data["paragraphs"].append(sub_line.strip())
                elif element.name in ['b', 'strong', 'h3', 'h4']:
                    t = element.get_text(" ", strip=True)
                    if t:
                        post_data["paragraphs"].append(f"<b>{html.escape(t)}</b>")
                else:
                    t = element.get_text(" ", strip=True)
                    if t:
                        for sub_line in t.split("\n"):
                            if sub_line.strip():
                                post_data["paragraphs"].append(html.escape(sub_line.strip()))

    # 5. Прикрепляем медиа из комментариев в конец списка медиа
    if comment_media:
        post_data["content"].extend(comment_media)

    return post_data


def build_rich_html(
    tags_html: str,
    paragraphs: list[str],
    comments: list[str],
    direct_url: str,
    inline_photo_urls: list[str] | None = None,
    partial_warning: bool = False
) -> str:
    """
    Собирает Rich HTML документ (Bot API 10.1–10.3):
    - Встраивает до 50 фотографий открытыми через <figure><img src="..."></figure>
    - Оставляет первые ~600 символов текста открытыми, а остальное прячет под <details>
    - Каждый комментарий выводит с новой строки в отдельном <p>
    - В самом низу всегда добавляет ссылку "Пост на JoyReactor" без эмодзи
    """
    blocks = []

    # 1. Теги (без эмодзи)
    if tags_html:
        blocks.append(f"<p>{tags_html}</p>")

    # 2. Открытые фото внутри Rich Message (до 50 штук)
    if inline_photo_urls:
        for photo_url in inline_photo_urls[:MAX_RICH_MEDIA]:
            blocks.append(f'<figure><img src="{html.escape(photo_url)}"></figure>')

    # 3. Основной текст (с разделением на открытую часть и спойлер <details>)
    if paragraphs:
        preview_p = []
        hidden_p = []
        current_len = 0

        for p in paragraphs:
            if current_len + len(p) > LIMIT_RICH_CHARS:
                break
            if not hidden_p and (current_len == 0 or current_len + len(p) <= PREVIEW_CHARS_LIMIT):
                preview_p.append(f"<p>{p}</p>")
            else:
                hidden_p.append(f"<p>{p}</p>")
            current_len += len(p)

        blocks.extend(preview_p)

        if hidden_p:
            hidden_html = "\n".join(hidden_p)
            blocks.append(
                f"<details><summary>Читать далее...</summary>\n{hidden_html}\n</details>"
            )

    # 4. Отличные комментарии (каждый комментарий в своем <p>, чтобы не слипались!)
    if comments:
        comments_p = "\n".join(f"<p>{c}</p>" for c in comments)
        blocks.append("<p>💬 <b>Отличные комментарии:</b></p>")
        if len(comments) > 3 or sum(len(c) for c in comments) > 400:
            blocks.append(f"<blockquote expandable>\n{comments_p}\n</blockquote>")
        else:
            blocks.append(f"<blockquote>\n{comments_p}\n</blockquote>")

    # 5. Предупреждение о пропущенных медиа (если > 50 или ошибка загрузки)
    if partial_warning:
        blocks.append("<p><i>Не все медиафайлы поместились, полный пост доступен по ссылке ниже.</i></p>")

    # 6. Ссылка на оригинал поста в самом конце (без эмодзи 🔗)
    if direct_url:
        blocks.append(f'<p><a href="{html.escape(direct_url)}">Пост на JoyReactor</a></p>')

    return "\n".join(blocks)


async def send_rich_message_to_telegram(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: str | int,
    tags_html: str,
    paragraphs: list[str],
    comments: list[str],
    direct_url: str,
    inline_photo_urls: list[str] | None = None,
    partial_warning: bool = False,
    reply_to_message_id: int | None = None
) -> tuple[bool, bool]:
    """
    Пытается отправить Rich Message через Bot API 10.1-10.3.
    Возвращает (success, photos_sent_inside_rich).
    Если сервер Telegram не поддерживает sendRichMessage или не может скачать картинки по URL,
    аккуратно откатывается на отправку текста без потери ссылки в конце.
    """
    url = f"https://api.telegram.org/bot{context.bot.token}/sendRichMessage"

    # Попытка 1: Отправляем Rich Message вместе со встроенными фото (до 50 шт), если они переданы
    if inline_photo_urls:
        rich_html_with_photos = build_rich_html(
            tags_html, paragraphs, comments, direct_url,
            inline_photo_urls=inline_photo_urls,
            partial_warning=partial_warning
        )
        payload = {"chat_id": chat_id, "rich_message": {"html": rich_html_with_photos}}
        if reply_to_message_id:
            payload["reply_parameters"] = {"message_id": reply_to_message_id, "allow_sending_without_reply": True}

        try:
            async with httpx.AsyncClient(timeout=45) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    logger.info(f"✅ Rich Message с {len(inline_photo_urls)} фото успешно отправлено")
                    return True, True
                logger.warning(f"⚠️ sendRichMessage с фото вернул {resp.status_code}: {resp.text}. Отправим фото отдельно.")
        except Exception as e:
            logger.warning(f"⚠️ Сбой сети при sendRichMessage с фото: {e}")
        return False, False

    # Попытка 2: Отправляем текстовый Rich Message (когда медиа уже отправлены открытыми выше или их нет)
    rich_html_text_only = build_rich_html(
        tags_html, paragraphs, comments, direct_url,
        inline_photo_urls=None,
        partial_warning=partial_warning
    )
    payload = {"chat_id": chat_id, "rich_message": {"html": rich_html_text_only}}
    if reply_to_message_id:
        payload["reply_parameters"] = {"message_id": reply_to_message_id, "allow_sending_without_reply": True}

    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                logger.info("✅ Текстовый Rich Message успешно отправлен")
                return True, False
            logger.warning(f"⚠️ sendRichMessage (текст) вернул {resp.status_code}: {resp.text}. Используем фолбэк.")
    except Exception as e:
        logger.warning(f"⚠️ Ошибка сети при sendRichMessage (текст): {e}. Используем фолбэк.")

    # Попытка 3 (Надежный фолбэк на стандартный HTML Telegram):
    # Гарантирует, что:
    # 1) Начало текста будет открытым, а продолжение — в сворачиваемой цитате <blockquote expandable>
    # 2) Комментарии НЕ слипнутся (разделены переносами \n\n)
    # 3) Ссылка "Пост на JoyReactor" НИКОГДА не обрежется (даже если пост на 25 000 символов!)
    await send_fallback_formatted_text(
        context=context,
        chat_id=chat_id,
        tags_html=tags_html,
        paragraphs=paragraphs,
        comments=comments,
        direct_url=direct_url,
        partial_warning=partial_warning,
        reply_to_message_id=reply_to_message_id
    )
    return True, False


async def send_fallback_formatted_text(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: str | int,
    tags_html: str,
    paragraphs: list[str],
    comments: list[str],
    direct_url: str,
    partial_warning: bool = False,
    reply_to_message_id: int | None = None
):
    """
    Форматированная отправка через стандартный send_message:
    Оставляет первые абзацы открытыми, остальное прячет в сворачиваемый блок <blockquote expandable>,
    сохраняет переносы между комментариями и гарантированно прикрепляет ссылку в самом конце.
    """
    link_html = f'<a href="{html.escape(direct_url)}">Пост на JoyReactor</a>'
    footer_parts = []
    if partial_warning:
        footer_parts.append("<i>Не все медиафайлы поместились, полный пост доступен по ссылке ниже.</i>")
    footer_parts.append(link_html)
    footer_block = "\n\n".join(footer_parts)

    header_block = f"{tags_html}\n\n" if tags_html else ""

    comments_block = ""
    if comments:
        # Двойной перенос строки \n\n гарантирует, что комментарии НЕ слипнутся!
        joined_comments = "\n\n".join(comments)
        comments_block = f"\n\n💬 <b>Отличные комментарии:</b>\n<blockquote expandable>{joined_comments}</blockquote>"

    # Сколько места остается под основной текст в одном сообщении (4096 символов)
    reserved_len = len(header_block) + len(comments_block) + len(footer_block) + 120
    available_body_len = max(800, LIMIT_TEXT_MSG - reserved_len)

    body_block = ""
    if paragraphs:
        preview_list = []
        expand_list = []
        used_len = 0
        is_truncated = False

        for p in paragraphs:
            if used_len + len(p) + 2 > available_body_len:
                # Добираем кусочек текущего абзаца, если место еще есть
                rem = available_body_len - used_len - 50
                if rem > 100:
                    expand_list.append(p[:rem].rsplit(" ", 1)[0] + "...")
                is_truncated = True
                break

            if not expand_list and (used_len == 0 or used_len + len(p) <= PREVIEW_CHARS_LIMIT):
                preview_list.append(p)
            else:
                expand_list.append(p)
            used_len += len(p) + 2

        preview_text = "\n\n".join(preview_list)
        if expand_list:
            expand_text = "\n\n".join(expand_list)
            if is_truncated:
                expand_text += f'\n\n<i>... (текст сокращен, продолжение в оригинале поста)</i>'
            if preview_text:
                body_block = f"{preview_text}\n\n<blockquote expandable>{expand_text}</blockquote>"
            else:
                body_block = f"<blockquote expandable>{expand_text}</blockquote>"
        else:
            body_block = preview_text

    final_message = f"{header_block}{body_block}{comments_block}\n\n{footer_block}".strip()

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=final_message,
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_to_message_id=reply_to_message_id
        )
    except Exception as e:
        logger.error(f"Text send error: {e}")


async def _process_media_group(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: str | int,
    post_id: str,
    media_objects: list,
    related_items: list,
    caption: str | None = None
) -> tuple[bool, int | None]:
    """
    Отправляет открытую медиагруппу чанками по 10 штук (с защитой от вечного цикла на TimedOut).
    """
    caption_sent_here = False
    first_msg_id = None

    while media_objects:
        chunk = media_objects[:MAX_MEDIA_PER_GROUP]
        chunk_items = related_items[:MAX_MEDIA_PER_GROUP]
        is_last_chunk = len(media_objects) <= MAX_MEDIA_PER_GROUP

        if is_last_chunk and caption:
            first_media = chunk[0]
            if isinstance(first_media, InputMediaPhoto):
                chunk[0] = InputMediaPhoto(media=first_media.media, caption=caption, parse_mode="HTML")
            elif isinstance(first_media, InputMediaVideo):
                chunk[0] = InputMediaVideo(media=first_media.media, caption=caption, parse_mode="HTML")

        try:
            sent_messages = await context.bot.send_media_group(chat_id=chat_id, media=chunk)
            if sent_messages and first_msg_id is None:
                first_msg_id = sent_messages[0].message_id

            if is_last_chunk and caption:
                caption_sent_here = True

            for item in chunk_items:
                item["send"] = "yes"

            media_objects = media_objects[MAX_MEDIA_PER_GROUP:]
            related_items = related_items[MAX_MEDIA_PER_GROUP:]

            if media_objects:
                await asyncio.sleep(3)

        except Exception as e:
            url_err = is_url_fetch_error(e)
            net_retry = is_retryable_network_error(e)

            # Защита от бесконечной переотправки при TimedOut во время выгрузки локальных файлов
            if isinstance(e, TimedOut) and all(it["send"] == "err" for it in chunk_items):
                logger.warning(
                    f"Post {post_id}: TimedOut при выгрузке локальных файлов. "
                    f"Помечаем чанк как отправленный во избежание дублей."
                )
                for item in chunk_items:
                    item["send"] = "yes"
                if is_last_chunk and caption:
                    caption_sent_here = True
                media_objects = media_objects[MAX_MEDIA_PER_GROUP:]
                related_items = related_items[MAX_MEDIA_PER_GROUP:]
                continue

            logger.error(f"Error in post {post_id}: {type(e).__name__} ({e})")

            for item in chunk_items:
                if item["send"] == "not":
                    item["send"] = "err" if (url_err or net_retry) else "close"
                elif item["send"] == "err":
                    send_retries = item.get("send_retries", 0) + 1
                    item["send_retries"] = send_retries
                    if not net_retry or send_retries >= MAX_SEND_RETRIES:
                        item["send"] = "close"

            if url_err:
                for item in related_items[MAX_MEDIA_PER_GROUP:]:
                    if item["send"] == "not":
                        item["send"] = "err"

            if net_retry:
                await asyncio.sleep(10)

            break

    return caption_sent_here, first_msg_id


async def send_post(context: ContextTypes.DEFAULT_TYPE, chat_id: str | int, post_id: str, post_data: dict):
    raw_content_list = post_data.get("content", [])
    tags_html = post_data.get("tags_html", "")
    paragraphs = post_data.get("paragraphs", [])
    comments = post_data.get("comments", [])

    direct_url = f"https://m.joyreactor.cc/post/{post_id}"
    link_post = f'<a href="{direct_url}">Пост на JoyReactor</a>'

    # 1. Ограничиваем общее количество медиа до 50 штук (лимит Rich Message), лишнее отрезаем
    truncated_by_limit = False
    if len(raw_content_list) > MAX_RICH_MEDIA:
        content_list = raw_content_list[:MAX_RICH_MEDIA]
        truncated_by_limit = True
        logger.info(f"Post {post_id}: Медиа обрезаны с {len(raw_content_list)} до {MAX_RICH_MEDIA} шт.")
    else:
        content_list = raw_content_list

    # 2. Если пост состоит только из фотографий и текста — пробуем отправить всё ОДНИМ Rich Message (до 50 фото внутри!)
    only_photos = bool(content_list) and all(item["type"] == "photo" for item in content_list)
    if only_photos and (paragraphs or comments or len(content_list) > MAX_MEDIA_PER_GROUP):
        photo_urls = [item["data"] for item in content_list]
        ok, sent_with_photos = await send_rich_message_to_telegram(
            context=context,
            chat_id=chat_id,
            tags_html=tags_html,
            paragraphs=paragraphs,
            comments=comments,
            direct_url=direct_url,
            inline_photo_urls=photo_urls,
            partial_warning=truncated_by_limit
        )
        if ok and sent_with_photos:
            return

    # 3. Определяем, влезет ли текст в короткую подпись к медиа или нужен отдельный текстовый блок
    has_media = bool(content_list)
    body_joined = "\n\n".join(paragraphs)
    comments_joined = "\n\n".join(comments)
    total_text_len = len(body_joined) + len(comments_joined)

    need_separate_text_msg = False
    post_caption = None

    if not has_media:
        need_separate_text_msg = True
    elif total_text_len == 0 and not truncated_by_limit:
        # В посте вообще нет текста и комментариев — только теги и ссылка под медиа
        post_caption = f"{tags_html}\n\n{link_post}" if tags_html else link_post
    elif total_text_len <= 400 and not comments and not truncated_by_limit:
        # Короткий текст без комментариев — отправляем прямо в подписи к медиа
        parts = [p for p in (tags_html, body_joined, link_post) if p]
        candidate = "\n\n".join(parts)
        if len(candidate) <= LIMIT_CAPTION:
            post_caption = candidate
        else:
            need_separate_text_msg = True
    else:
        # Длинный текст или есть комментарии -> медиа идут открытыми, а текст и теги — следом
        need_separate_text_msg = True

    caption_sent = not bool(post_caption)
    everything_sent = not truncated_by_limit
    first_media_msg_id = None

    try:
        while True:
            pending_items = [c for c in content_list if c["send"] not in ("yes", "close")]
            if not pending_items:
                break

            download_retry_occurred = False
            photo_group, photo_items = [], []
            video_group, video_items = [], []
            gif_group, gif_items = [], []
            video_hosting_group = []

            for content in content_list:
                if content["send"] == "yes":
                    continue
                if content["send"] == "close":
                    everything_sent = False
                    continue

                media_type = content["type"]

                # Скачиваем локально (и нарезаем, если комикс) только 1 раз
                if content["send"] == "err" and media_type in ("photo", "video", "gif"):
                    if "local_files" not in content:
                        files = await download_media(content["data"])
                        if files:
                            content["local_files"] = files
                        else:
                            retries = content.get("dl_retries", 0) + 1
                            content["dl_retries"] = retries
                            if retries <= 3:
                                download_retry_occurred = True
                                logger.info(f"Retrying download for {content['data']} (Attempt {retries}/3)")
                            else:
                                content["send"] = "close"
                                everything_sent = False
                            continue

                if media_type == "photo":
                    if content["send"] == "not":
                        photo_group.append(InputMediaPhoto(media=content["data"], parse_mode="HTML"))
                        photo_items.append(content)
                    elif content["send"] == "err" and content.get("local_files"):
                        for f_path in content["local_files"]:
                            if os.path.exists(f_path):
                                with open(f_path, "rb") as f:
                                    photo_group.append(InputMediaPhoto(media=f.read(), parse_mode="HTML"))
                                    photo_items.append(content)

                elif media_type == "video":
                    if content["send"] == "not":
                        video_group.append(InputMediaVideo(media=content["data"], parse_mode="HTML"))
                        video_items.append(content)
                    elif content["send"] == "err" and content.get("local_files"):
                        for f_path in content["local_files"]:
                            if os.path.exists(f_path):
                                with open(f_path, "rb") as f:
                                    video_group.append(InputMediaVideo(media=f.read(), parse_mode="HTML"))
                                    video_items.append(content)

                elif media_type == "gif":
                    if content["send"] == "not":
                        gif_group.append(content["data"])
                        gif_items.append(content)
                    elif content["send"] == "err" and content.get("local_files"):
                        for f_path in content["local_files"]:
                            if os.path.exists(f_path):
                                with open(f_path, "rb") as f:
                                    gif_group.append(f.read())
                                    gif_items.append(content)

                elif media_type == "video_hosting":
                    video_hosting_group.append(content)

            # Если после нарезки длинных комиксов файлов стало больше 50, обрезаем до 50
            if len(photo_group) > MAX_RICH_MEDIA:
                photo_group = photo_group[:MAX_RICH_MEDIA]
                photo_items = photo_items[:MAX_RICH_MEDIA]
                everything_sent = False

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
                sent, msg_id = await _process_media_group(
                    context, chat_id, post_id, photo_group, photo_items,
                    caption=post_caption if last_media_type == "photo" and not caption_sent else None
                )
                if sent:
                    caption_sent = True
                if msg_id and first_media_msg_id is None:
                    first_media_msg_id = msg_id

            if video_group:
                sent, msg_id = await _process_media_group(
                    context, chat_id, post_id, video_group, video_items,
                    caption=post_caption if last_media_type == "video" and not caption_sent else None
                )
                if sent:
                    caption_sent = True
                if msg_id and first_media_msg_id is None:
                    first_media_msg_id = msg_id

            if gif_group:
                for i, (gif_media, item) in enumerate(zip(gif_group, gif_items)):
                    is_last_gif = (i == len(gif_group) - 1)
                    current_caption = post_caption if is_last_gif and last_media_type == "gif" and not caption_sent else None

                    try:
                        msg = await context.bot.send_animation(
                            chat_id=chat_id,
                            animation=gif_media,
                            caption=current_caption,
                            parse_mode="HTML"
                        )
                        if first_media_msg_id is None:
                            first_media_msg_id = msg.message_id
                        logger.info(f"Post {post_id} - Sent gif")
                        item["send"] = "yes"
                        if current_caption:
                            caption_sent = True
                    except Exception as e:
                        if isinstance(e, TimedOut) and item["send"] == "err":
                            logger.warning(f"Post {post_id}: TimedOut при отправке локального gif, помечаем отправленным.")
                            item["send"] = "yes"
                            continue

                        url_err = is_url_fetch_error(e)
                        net_retry = is_retryable_network_error(e)
                        logger.error(f"Error in post {post_id} (gif): {e}")

                        if item["send"] == "not":
                            item["send"] = "err" if (url_err or net_retry) else "close"
                        elif item["send"] == "err":
                            send_retries = item.get("send_retries", 0) + 1
                            item["send_retries"] = send_retries
                            if not net_retry or send_retries >= MAX_SEND_RETRIES:
                                item["send"] = "close"
                        if net_retry:
                            await asyncio.sleep(10)

            if video_hosting_group:
                for i, item in enumerate(video_hosting_group):
                    is_last_vh = (i == len(video_hosting_group) - 1)
                    current_caption = post_caption if is_last_vh and last_media_type == "video_hosting" and not caption_sent else None

                    text = f'<a href="{item["data"]}">📺 Смотреть видео</a>'
                    if current_caption:
                        text += f"\n\n{current_caption}"

                    try:
                        msg = await context.bot.send_message(
                            chat_id=chat_id,
                            text=text,
                            parse_mode="HTML",
                            link_preview_options=LinkPreviewOptions(is_disabled=False)
                        )
                        if first_media_msg_id is None:
                            first_media_msg_id = msg.message_id
                        item["send"] = "yes"
                        if current_caption:
                            caption_sent = True
                    except Exception as e:
                        if "webpage" in str(e).lower():
                            try:
                                msg = await context.bot.send_message(
                                    chat_id=chat_id,
                                    text=text,
                                    parse_mode="HTML",
                                    link_preview_options=LinkPreviewOptions(is_disabled=True)
                                )
                                if first_media_msg_id is None:
                                    first_media_msg_id = msg.message_id
                                item["send"] = "yes"
                                if current_caption:
                                    caption_sent = True
                            except Exception as inner_e:
                                logger.error(f"Fallback error: {inner_e}")
                                item["send"] = "close"
                        else:
                            net_retry = is_retryable_network_error(e)
                            send_retries = item.get("send_retries", 0) + 1
                            item["send_retries"] = send_retries
                            if not net_retry or send_retries >= MAX_SEND_RETRIES:
                                item["send"] = "close"
                            else:
                                await asyncio.sleep(10)

            if download_retry_occurred:
                await asyncio.sleep(5)

        if any(c["send"] == "close" for c in content_list):
            everything_sent = False

        # 4. Отправляем текстовую часть (или уведомление о неполной загрузке)
        if need_separate_text_msg or not everything_sent:
            await send_rich_message_to_telegram(
                context=context,
                chat_id=chat_id,
                tags_html=tags_html,
                paragraphs=paragraphs,
                comments=comments,
                direct_url=direct_url,
                inline_photo_urls=None,
                partial_warning=(not everything_sent),
                reply_to_message_id=first_media_msg_id
            )

    finally:
        await clear_data_folder()


async def fetch_html(url: str) -> str:
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status == 200:
                    return await response.text()
                else:
                    logger.error(f"Site loading error ({url}): {response.status}")
                    return ""
    except Exception as e:
        logger.error(f"Connection error to {url}: {e}")
        return ""


async def monitor_website(context: ContextTypes.DEFAULT_TYPE):
    global recorded_sent_posts
    post_id = "0"
    has_new_posts = False

    try:
        for url in URLS:
            html_text = await fetch_html(url)
            if not html_text:
                continue

            chat_id = TELEGRAM_CHAT_V if url in URLS_V else TELEGRAM_CHAT_PL
            soup = BeautifulSoup(html_text, "html.parser")
            posts = soup.find_all("div", class_="postContainer")

            for post in posts:
                post_id_full = post.get("id")
                if not post_id_full:
                    continue
                post_id = post_id_full.split("postContainer")[-1].strip('"')

                if post_id not in processed_posts[url]:
                    processed_posts[url].append(post_id)
                    has_new_posts = True
                    recorded_sent_posts = True

                    post_data = parse_joy_post(post)
                    if post_data and (post_data["content"] or post_data["paragraphs"] or post_data["comments"]):
                        try:
                            await send_post(context, chat_id=chat_id, post_id=post_id, post_data=post_data)
                        except Exception as send_err:
                            logger.error(f"Unhandled error sending post {post_id}: {send_err}")

                        await save_sent_posts(context.application)
                        recorded_sent_posts = False
                        await asyncio.sleep(5)

        if not has_new_posts and recorded_sent_posts:
            await save_sent_posts(context.application)
            recorded_sent_posts = False

    except Exception as e:
        logger.error(f"Error in monitor_website (last post {post_id}): {e}")


if __name__ == "__main__":
    rate_limiter = AIORateLimiter(
        overall_max_rate=30,
        overall_time_period=1,
        group_time_period=60,
        max_retries=3
    )

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(request)
        .rate_limiter(rate_limiter)
        .post_init(load_sent_posts)
        .post_shutdown(save_sent_posts)
        .build()
    )

    application.job_queue.run_repeating(monitor_website, interval=60, first=1)

    logger.info("Bot started via PTB Application")
    application.run_polling(drop_pending_updates=True)