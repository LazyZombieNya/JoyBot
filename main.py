import asyncio
import html
import logging
import os
import pickle
import sys
import uuid
from collections import deque, defaultdict
from urllib.parse import urlparse, urlsplit, urlunsplit, quote, unquote

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

# Загружаем переменные окружения из файла .env (токены, ссылки, настройки)
load_dotenv()

# Чтение основных настроек из переменных окружения
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_V = os.getenv("TELEGRAM_CHAT_V") # ID первого чата
TELEGRAM_CHAT_PL = os.getenv("TELEGRAM_CHAT_PL") # ID второго чата

# Парсинг списков URL-адресов, которые бот будет мониторить
URLS_V = [u.strip() for u in os.getenv("URLS_V", "").split(",") if u.strip()]
URLS_PL = [u.strip() for u in os.getenv("URLS_PL", "").split(",") if u.strip()]
URLS = URLS_V + URLS_PL

# Список тегов, посты с которыми бот должен игнорировать
UNWANTED_TAGS = [t.strip() for t in os.getenv("UNWANTED_TAGS", "").split(",") if t.strip()]

# Проверка наличия обязательных переменных
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_V:
    raise ValueError("Error: (.env) Environment variables not set!")

# Настройка системы логирования для вывода информации в консоль
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
# Отключаем лишний спам от сторонних библиотек
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger("JoyBot")
HEADERS = {"User-Agent": "Mozilla/5.0"} # Заголовок для обхода блокировок при парсинге

# Настройка HTTP-клиента для Telegram API (увеличиваем тайм-ауты для тяжелых файлов)
request = HTTPXRequest(
    connect_timeout=60,
    read_timeout=120,
    write_timeout=120,
    pool_timeout=60
)

# --- Блок лимитов и ограничений Telegram ---
MAX_RICH_MEDIA = 50       # Макс. количество медиа в одном Rich-сообщении
LIMIT_RICH_CHARS = 30000  # Макс. длина текста в Rich Message
PREVIEW_MAX_MEDIA = 4     # Макс. количество медиа до скрытия под кат (спойлер)
PREVIEW_MAX_CHARS = 600   # Макс. длина текста до скрытия под кат
LIMIT_TEXT_MSG = 4000     # Лимит обычного текстового сообщения Telegram
MAX_MEDIA_PER_GROUP = 10  # Макс. количество файлов в медиагруппе Telegram
MAX_SEND_RETRIES = 2      # Количество попыток отправки при ошибке

# --- Настройки сохранения состояния ---
SAVE_FILE = "sent_posts.pkl" # Файл для сохранения отправленных ID постов
MAX_POSTS = 50               # Храним историю последних 50 постов для каждого URL
# Словарь для хранения истории (чтобы не отправлять одни и те же посты после перезапуска)
processed_posts = defaultdict(lambda: deque(maxlen=MAX_POSTS))
recorded_sent_posts = False


async def load_sent_posts(app: Application):
    """Асинхронная функция загрузки истории отправленных постов из файла."""
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
    """Асинхронная функция сохранения текущей истории постов в файл (чтобы не потерять при выключении)."""
    normal_dict = {key: list(value) for key, value in processed_posts.items()}
    try:
        async with aiofiles.open(SAVE_FILE, "wb") as file:
            await file.write(pickle.dumps(normal_dict))
    except Exception as e:
        logger.error(f"Error while saving sent posts: {e}")


def is_url_fetch_error(e: Exception) -> bool:
    """Проверяет, связана ли ошибка с тем, что Telegram не смог скачать файл по переданной ссылке."""
    text = str(e).lower()
    url_errors = [
        "webpage_media_empty",
        "webpage_curl_failed",
        "wrong file identifier/http url specified",
        "failed to get http url content",
    ]
    return any(t in text for t in url_errors)


def is_retryable_network_error(e: Exception) -> bool:
    """Проверяет, является ли ошибка временной сетевой проблемой (при которой стоит попробовать еще раз)."""
    if isinstance(e, (TimedOut, NetworkError, RetryAfter)):
        return True
    text = str(e).lower()
    retry_texts = [
        "timed out", "timeout", "networkerror", "connecterror",
        "all connection attempts failed", "cannot connect to host",
        "getaddrinfo failed", "connection reset", "server disconnected",
        "retry after", "flood control", "bad gateway",
        "gateway timeout", "internal server error",
    ]
    return any(t in text for t in retry_texts)


def normalize_url(url: str) -> str:
    """Приводит спарсенные ссылки к правильному формату (добавляет https, кодирует спецсимволы)."""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https://joyreactor.cc" + url

    try:
        parts = urlsplit(url)
        clean_path = quote(unquote(parts.path), safe="/:@-._~")
        clean_query = quote(unquote(parts.query), safe="=&?/:@-._~")
        return urlunsplit((parts.scheme, parts.netloc, clean_path, clean_query, parts.fragment))
    except Exception:
        return url


def clean_iframe_url(iframe_url: str) -> str:
    """Очищает и адаптирует ссылки на сторонние плееры (YouTube, Coub, Vimeo) для нормального отображения в Telegram."""
    parsed = urlparse(iframe_url)
    clean_url = f"https://{parsed.netloc}{parsed.path}"
    if "youtube.com" in clean_url:
        clean_url = clean_url.replace("/embed/", "/watch?v=")
    elif "coub.com" in clean_url:
        clean_url = clean_url.replace("/embed/", "/view/")
    elif "vimeo.com" in clean_url:
        clean_url = clean_url.replace("/video/", "/")
    return clean_url


def extract_media_from_image_div(img_div: Tag) -> list[dict]:
    """
    Анализирует HTML-блок с картинкой/видео/гифкой.
    Находит все медиаисточники и возвращает список словарей с их типами и URL-адресами.
    """
    found = []
    has_specific = False

    # Поиск обычных видео
    for video_span in img_div.find_all('span', class_='video_holder'):
        has_specific = True
        source = video_span.find('source', type="video/mp4") or video_span.find('source')
        if source and source.get('src'):
            url = normalize_url(source.get('src'))
            found.append({"id": str(uuid.uuid4()), "type": "video", "data": url, "send": "not"})

    # Поиск GIF-анимаций (на JoyReactor они часто перекодированы в MP4)
    for gif_span in img_div.find_all('span', class_='video_gif_holder'):
        has_specific = True
        source = gif_span.find('source', type="video/mp4")
        a_tag = gif_span.find('a')
        if source and source.get('src'):
            url = normalize_url(source.get('src'))
            found.append({"id": str(uuid.uuid4()), "type": "video", "data": url, "send": "not"})
        elif a_tag and a_tag.get('href'):
            url = normalize_url(a_tag.get('href'))
            found.append({"id": str(uuid.uuid4()), "type": "gif", "data": url, "send": "not"})

    # Поиск встроенных плееров (iframe)
    for iframe in img_div.find_all('iframe'):
        has_specific = True
        iframe_url = iframe.get('src')
        if iframe_url:
            c_url = clean_iframe_url(iframe_url)
            found.append({"id": str(uuid.uuid4()), "type": "video_hosting", "data": c_url, "send": "not"})

    if has_specific:
        return found

    # Резервный поиск обычных картинок (полное изображение и превью)
    a_full = img_div.find('a', href=True)
    img_el = img_div.find('img', src=True)

    full_url = normalize_url(a_full['href']) if a_full else ""
    preview_url = normalize_url(img_el['src']) if img_el else ""

    chosen_full = full_url or preview_url
    chosen_preview = preview_url or full_url

    if chosen_full:
        found.append({
            "id": str(uuid.uuid4()),
            "type": "photo",
            "data": chosen_full,
            "preview_url": chosen_preview,
            "send": "not"
        })

    return found


def format_inline_element(node) -> str:
    """
    Рекурсивно проходит по HTML-тегам внутри текста и переводит их в формат,
    который поддерживает Telegram (жирный, курсив, ссылки).
    """
    if isinstance(node, NavigableString):
        return html.escape(str(node))
    if not isinstance(node, Tag):
        return ""

    if node.name == "br":
        return "\n"
    if node.name == "a":
        href = node.get("href", "")
        text = node.get_text(strip=True)
        if href and text:
            if len(text) > 40:
                text = text[:37] + "..."
            return f'<a href="{html.escape(normalize_url(href))}">{html.escape(text)}</a>'
        return html.escape(text)
    if node.name in ("b", "strong"):
        inner = "".join(format_inline_element(c) for c in node.children).strip()
        return f"<b>{inner}</b>" if inner else ""
    if node.name in ("i", "em"):
        inner = "".join(format_inline_element(c) for c in node.children).strip()
        return f"<i>{inner}</i>" if inner else ""
    if node.name in ("figure", "img", "video", "p"):
        return str(node)

    return "".join(format_inline_element(c) for c in node.children)


def extract_flow_from_container(container: Tag, flow_blocks: list[dict], media_list: list[dict]):
    """
    Основная логика парсинга контента поста. Проходит по дереву HTML и разбивает
    содержимое на блоки (текст, заголовки, медиа), игнорируя мусорные элементы.
    """
    for child in container.children:
        # Обработка чистого текста без тегов
        if isinstance(child, NavigableString):
            text = str(child).strip()
            if text:
                for line in text.split("\n"):
                    if line.strip():
                        flow_blocks.append(
                            {"kind": "text", "html": html.escape(line.strip()), "chars": len(line.strip())})
            continue

        if not isinstance(child, Tag):
            continue

        classes = child.get("class") or []

        # Игнорируем кнопки, скрипты, элементы управления
        if (
                "collapse-content-button" in classes
                or "expandable-post-content" in classes
                or "post-footer" in classes
                or "post-header" in classes
                or "post-tags" in classes
                or "absolute" in classes
                or child.name in ("button", "svg", "script", "style")
        ):
            continue

        # Обработка изображений/видео
        if "image" in classes:
            extracted = extract_media_from_image_div(child)
            for m in extracted:
                media_list.append(m)
                flow_blocks.append({"kind": "media", "media": m})
            continue

        # Обработка вложенных iframe
        if child.name == "iframe":
            iframe_url = child.get("src")
            if iframe_url:
                c_url = clean_iframe_url(iframe_url)
                m = {"id": str(uuid.uuid4()), "type": "video_hosting", "data": c_url, "send": "not"}
                media_list.append(m)
                flow_blocks.append({"kind": "media", "media": m})
            continue

        # Обработка заголовков
        if child.name in ("h1", "h2", "h3", "h4"):
            heading_text = child.get_text(" ", strip=True)
            if heading_text:
                flow_blocks.append({
                    "kind": "heading",
                    "html": f"<h3>{html.escape(heading_text)}</h3>",
                    "fallback_html": f"<b>{html.escape(heading_text)}</b>",
                    "chars": len(heading_text)
                })
            continue

        # Если внутри блока есть другие блоки - рекурсивный вызов
        has_nested_media_or_blocks = (
                child.find("div", class_="image") is not None
                or child.find(["h1", "h2", "h3", "h4", "iframe", "table", "p"]) is not None
                or child.name in ("table", "tbody", "thead", "tr", "td", "th", "article", "section")
                or "post-content" in classes
                or "post_content" in classes
                or "expand-wrapper" in classes
                or "single" in classes
        )
        if has_nested_media_or_blocks and child.name not in ("a", "b", "strong", "i", "em"):
            extract_flow_from_container(child, flow_blocks, media_list)
            continue

        # Финальная обработка инлайн-элементов (ссылки, жирный текст и т.д.)
        inline_html = "".join(format_inline_element(c) for c in child.children).strip()
        if inline_html:
            for line in inline_html.split("\n"):
                clean_line = line.strip()
                if clean_line:
                    plain_len = len(BeautifulSoup(clean_line, "html.parser").get_text())
                    flow_blocks.append({"kind": "text", "html": clean_line, "chars": plain_len})


def extract_post_tags(post: Tag) -> list[str] | None:
    """Извлекает теги из поста. Если найден тег из черного списка (UNWANTED_TAGS), возвращает None, чтобы пропустить пост."""
    tags = []
    post_tags_div = post.find("div", class_=lambda c: c and "post-tags" in c)
    if post_tags_div:
        for a_tag in post_tags_div.find_all("a"):
            t = a_tag.get_text(strip=True)
            if t and t != "...":
                tags.append(t)
        post_tags_div.decompose() # Удаляем теги из DOM дерева, чтобы они не попали в текст поста

    # Дополнительный поиск тегов в заголовках H2
    for h2 in post.find_all("h2"):
        raw_text = h2.get_text(", ", strip=True)
        if raw_text:
            for t in raw_text.split(","):
                t_clean = t.strip()
                if t_clean and t_clean != "..." and t_clean not in tags:
                    tags.append(t_clean)
        h2.decompose()

    # Проверка по блэклисту
    for t in tags:
        if any(unwanted.lower() in t.lower() for unwanted in UNWANTED_TAGS if unwanted):
            return None

    return tags


def extract_best_comments(post: Tag, comment_media: list) -> list[str]:
    """Извлекает блок 'Отличный комментарий' из поста, собирает авторов, текст и медиа внутри комментов."""
    comments_text_list = []
    candidate_comments = []

    # Поиск комментариев по специфичным для сайта ID или заголовкам
    old_best = post.find_all('div', id=lambda x: x and x.startswith('best_comment_'))
    if old_best:
        candidate_comments.extend(old_best)
    else:
        best_header = post.find(lambda tag: tag.name in ("h3", "div") and "Отличный комментарий" in tag.get_text())
        if best_header:
            branch_root = best_header.find_parent("div", class_="pt-1") or best_header.find_parent("div")
            if branch_root:
                candidate_comments.extend(branch_root.find_all("div", class_=lambda c: c and "comment" in c.split()))

    if candidate_comments:
        for comment in candidate_comments:
            # Игнорируем скрытые (заминусованные) комментарии
            if comment.find("div", class_="comment-hidden"):
                continue
            content_div = comment.find('div', class_='comment-content')
            if not content_div:
                continue
            if "Нажмите, чтобы показать" in content_div.get_text() or "Коммент скрыт" in content_div.get_text():
                continue

            # Получение имени автора
            author = "Аноним"
            user_block = comment.find(class_='comment-username') or comment.find(class_='comment_username')
            if user_block:
                if user_block.name == "a":
                    author = html.escape(user_block.get_text(strip=True))
                else:
                    links = user_block.find_all("a")
                    for l in links:
                        txt = l.get_text(strip=True)
                        if txt:
                            author = html.escape(txt)
                            break

            # Обработка видео (iframe) внутри комментариев
            for iframe in content_div.find_all('iframe'):
                iframe_url = iframe.get('src')
                if iframe_url:
                    c_url = clean_iframe_url(iframe_url)
                    comment_media.append(
                        {"id": str(uuid.uuid4()), "type": "video_hosting", "data": c_url, "send": "not"})
                    new_tag = BeautifulSoup(f'<p>📺 <a href="{html.escape(c_url)}">Смотреть видео</a></p>',
                                            "html.parser")
                    iframe.replace_with(new_tag)
                else:
                    iframe.decompose()

            # Обработка картинок/гифок внутри комментариев
            for img_div in content_div.find_all('div', class_='image'):
                extracted = extract_media_from_image_div(img_div)
                media_htmls = ["<br>"]
                for m in extracted:
                    comment_media.append(m)
                    url = html.escape(m.get("preview_url", m["data"]))
                    # Формируем HTML для отображения картинки в комментарии
                    if m["type"] in ("photo", "gif"):
                        media_htmls.append(f'<figure><img src="{url}"></figure>')
                    elif m["type"] == "video":
                        media_htmls.append(f'<figure><video src="{url}"></video></figure>')
                    elif m["type"] == "video_hosting":
                        media_htmls.append(f'<p>📺 <a href="{url}">Смотреть видео</a></p>')

                if len(media_htmls) > 1:
                    new_tag = BeautifulSoup("".join(media_htmls), "html.parser")
                    img_div.replace_with(new_tag)
                else:
                    img_div.decompose()

            # Преобразование текста комментария
            comment_html = "".join(format_inline_element(c) for c in content_div.children).strip()
            comment_html = "\n".join(line for line in comment_html.split("\n") if line.strip())

            if comment_html:
                entry = f"👤 <b>{author}</b>: {comment_html}"
                if entry not in comments_text_list:
                    comments_text_list.append(entry)

    if comments_text_list:
        logger.info(f"Парсинг: Найдено отличных комментариев - {len(comments_text_list)}")

    # Удаляем деревья комментариев, чтобы они не попали в основной текст поста
    for comment_list in post.find_all('div', class_='post_comment_list'):
        comment_list.decompose()
    for c_div in post.find_all('div', class_=lambda c: c and (
            'comment' in c.split() or 'comment-with-indentation' in c.split())):
        c_div.decompose()

    return comments_text_list


def parse_joy_post(post: Tag) -> dict | None:
    """Центральная функция парсинга: собирает теги, текст, медиа и комментарии в единый словарь."""
    tags_list = extract_post_tags(post)
    if tags_list is None:
        return None # Если в посте есть запрещенный тег, пропускаем его

    post_data = {
        "tags_html": ", ".join(f"<code>{html.escape(t)}</code>" for t in tags_list) if tags_list else "",
        "flow_blocks": [],
        "comments": [],
        "content": []
    }

    post_data["comments"] = extract_best_comments(post, post_data["content"])

    post_content_div = (
            post.find('div', class_='post-content')
            or post.find('div', class_='post_content')
            or post
    )
    extract_flow_from_container(post_content_div, post_data["flow_blocks"], post_data["content"])

    return post_data


def render_media_block_html(media_item: dict, use_preview_url: bool = True) -> str:
    """Вспомогательная функция для создания HTML-тегов для Rich Messages (figure, img, video)."""
    m_type = media_item["type"]
    url = html.escape(media_item.get("preview_url", media_item["data"]) if use_preview_url else media_item["data"])

    if m_type == "photo":
        return f'<figure><img src="{url}"></figure>'
    elif m_type == "gif":
        return f'<figure><img src="{url}"></figure>'
    elif m_type == "video":
        return f'<figure><video src="{url}"></video></figure>'
    elif m_type == "video_hosting":
        return f'<p>📺 <a href="{url}">Смотреть видео</a></p>'
    return ""


def split_long_paragraph_html(html_text: str, max_len: int = PREVIEW_MAX_CHARS) -> tuple[str, str]:
    """Разбивает длинный параграф текста на две части, чтобы спрятать хвост под кат <details>."""
    if len(html_text) <= max_len:
        return html_text, ""
    split_pos = html_text.rfind(" ", int(max_len * 0.5), max_len)
    if split_pos == -1:
        split_pos = max_len

    open_a = html_text.rfind("<a ", 0, split_pos)
    close_a = html_text.rfind("</a>", 0, split_pos)
    if open_a != -1 and (close_a == -1 or open_a > close_a):
        end_a = html_text.find("</a>", split_pos)
        if end_a != -1:
            split_pos = end_a + 4

    return html_text[:split_pos].strip() + "...", html_text[split_pos:].strip()


def build_rich_html(
    post_data: dict,
    direct_url: str,
    use_preview_urls: bool = True,
    include_media_inline: bool = True,
    partial_warning: bool = False
) -> tuple[str, int, int]:
    """
    Генерирует сложный HTML-код для нового формата сообщений Telegram (Rich Media).
    Распределяет медиа и текст на видимую часть и скрытую (внутри тега <details>).
    """
    flow_blocks = post_data.get("flow_blocks", [])
    tags_html = post_data.get("tags_html", "")
    comments = post_data.get("comments", [])

    visible_html_parts = []
    hidden_html_parts = []

    shown_media_count = 0
    total_media_count = 0
    expected_cdn_media = 0
    hidden_media_count = 0

    shown_chars_count = 0
    hidden_chars_count = 0
    total_chars_count = 0

    is_folding = False
    truncated_by_50 = False

    for block in flow_blocks:
        kind = block["kind"]

        if kind == "media":
            if not include_media_inline:
                continue

            if total_media_count >= MAX_RICH_MEDIA:
                truncated_by_50 = True
                continue

            media_html = render_media_block_html(block["media"], use_preview_url=use_preview_urls)
            if not media_html:
                continue

            total_media_count += 1
            if block["media"]["type"] in ("photo", "video", "gif"):
                expected_cdn_media += 1

            # Если превышен лимит отображаемых медиа - прячем остальные в <details>
            if is_folding or shown_media_count >= PREVIEW_MAX_MEDIA:
                is_folding = True
                hidden_html_parts.append(media_html)
                hidden_media_count += 1
            else:
                visible_html_parts.append(media_html)
                shown_media_count += 1

        elif kind in ("heading", "text"):
            b_chars = block.get("chars", 0)
            if total_chars_count + b_chars > LIMIT_RICH_CHARS:
                break
            total_chars_count += b_chars

            if kind == "heading":
                if is_folding:
                    hidden_html_parts.append(block["html"])
                    hidden_chars_count += b_chars
                else:
                    visible_html_parts.append(block["html"])
                    shown_chars_count += b_chars
            else:
                # Логика разбивки слишком длинного текста
                if not is_folding and shown_chars_count + b_chars <= PREVIEW_MAX_CHARS:
                    visible_html_parts.append(f"<p>{block['html']}</p>")
                    shown_chars_count += b_chars
                elif not is_folding and shown_chars_count == 0 and b_chars > PREVIEW_MAX_CHARS:
                    head_part, tail_part = split_long_paragraph_html(block["html"], PREVIEW_MAX_CHARS)
                    visible_html_parts.append(f"<p>{head_part}</p>")
                    shown_chars_count += len(head_part)
                    if tail_part:
                        is_folding = True
                        hidden_html_parts.append(f"<p>{tail_part}</p>")
                        hidden_chars_count += len(tail_part)
                else:
                    is_folding = True
                    hidden_html_parts.append(f"<p>{block['html']}</p>")
                    hidden_chars_count += b_chars

    # Сборка финального HTML-кода
    result_blocks = []
    result_blocks.extend(visible_html_parts)

    if hidden_html_parts:
        summary_stats = []
        if hidden_media_count > 0:
            summary_stats.append(f"еще {hidden_media_count} медиа")
        if hidden_chars_count > 0:
            summary_stats.append(f"{hidden_chars_count} симв. текста")

        stats_str = f" ({', '.join(summary_stats)})" if summary_stats else ""
        summary_title = f"Читать далее{stats_str}..." if hidden_chars_count > 0 else f"Показать остальные медиа{stats_str}..."

        hidden_joined = "\n".join(hidden_html_parts)
        result_blocks.append(f"<details><summary>{summary_title}</summary>\n{hidden_joined}\n</details>")

    if tags_html:
        result_blocks.append(f"<p>{tags_html}</p>")

    if comments:
        comments_p = "\n<br>\n".join(f"<p>{c}</p>" for c in comments)
        result_blocks.append("<p>💬 <b>Отличные комментарии:</b></p>")
        if len(comments) > 3 or sum(len(c) for c in comments) > 400 or "<figure>" in comments_p:
            result_blocks.append(f"<blockquote expandable>\n{comments_p}\n</blockquote>")
        else:
            result_blocks.append(f"<blockquote>\n{comments_p}\n</blockquote>")

    if partial_warning or truncated_by_50:
        result_blocks.append("<p><i>Не все медиафайлы поместились (лимит 50 шт.), полный пост доступен по ссылке ниже.</i></p>")

    if direct_url:
        result_blocks.append(f'<p><a href="{html.escape(direct_url)}">Пост на JoyReactor</a></p>')

    return "\n".join(result_blocks), expected_cdn_media, total_media_count


def verify_rich_message_has_media(resp_json: dict) -> bool:
    """Проверяет ответ Telegram API, чтобы убедиться, что сервер успешно обработал и скачал переданные URL картинок."""
    try:
        msg_res = resp_json.get("result", {})
        rich_msg = msg_res.get("rich_message", {})
        raw_str = str(rich_msg).lower()
        for marker in ("figure", "photo", "image", "media", "video", "file_id"):
            if marker in raw_str:
                return True
        return False
    except Exception:
        return True


async def send_rich_message_to_telegram(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: str | int,
    post_data: dict,
    direct_url: str,
    include_media_inline: bool = True,
    partial_warning: bool = False,
    reply_to_message_id: int | None = None
) -> tuple[bool, bool]:
    """
    Отправляет собранный HTML-код в Telegram как специальный Rich Message
    через прямой POST-запрос к Telegram Bot API.
    Возвращает (успех_отправки, были_ли_медиа_в_сообщении).
    """
    url = f"https://api.telegram.org/bot{context.bot.token}/sendRichMessage"

    # Попытка отправки сообщения с медиа
    if include_media_inline and post_data.get("content"):
        # Пробуем дважды: с превью-ссылками и полными ссылками
        for use_preview in (True, False):
            rich_html, expected_cdn_media, total_media = build_rich_html(
                post_data=post_data,
                direct_url=direct_url,
                use_preview_urls=use_preview,
                include_media_inline=True,
                partial_warning=partial_warning
            )
            payload = {"chat_id": chat_id, "rich_message": {"html": rich_html}}
            if reply_to_message_id:
                payload["reply_parameters"] = {"message_id": reply_to_message_id, "allow_sending_without_reply": True}

            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 200:
                        resp_data = resp.json()
                        # Если Telegram вернул 200 OK, но не смог загрузить медиа (ошибка CDN) — удаляем и будем грузить локально
                        if expected_cdn_media > 0 and not verify_rich_message_has_media(resp_data):
                            sent_msg_id = resp_data.get("result", {}).get("message_id")
                            logger.warning(
                                f"⚠️ Telegram принял Rich Message, но НЕ прогрузил "
                                f"картинки/видео с CDN. Удаляем и качаем локально!"
                            )
                            if sent_msg_id:
                                try:
                                    await context.bot.delete_message(chat_id=chat_id, message_id=sent_msg_id)
                                except Exception:
                                    pass
                            continue

                        logger.info(f"✅ Rich Message с {total_media} медиа успешно отправлено")
                        return True, True
                    logger.warning(f"⚠️ sendRichMessage вернул {resp.status_code}: {resp.text}")
            except Exception as e:
                logger.warning(f"⚠️ Ошибка сети при sendRichMessage с медиа: {e}")

        return False, False

    # Попытка отправки только текста (если медиа не получилось загрузить)
    rich_html_text, _, _ = build_rich_html(
        post_data=post_data,
        direct_url=direct_url,
        include_media_inline=False,
        partial_warning=partial_warning
    )
    payload = {"chat_id": chat_id, "rich_message": {"html": rich_html_text}}
    if reply_to_message_id:
        payload["reply_parameters"] = {"message_id": reply_to_message_id, "allow_sending_without_reply": True}

    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                logger.info("✅ Текстовый Rich Message успешно отправлен")
                return True, False
            logger.warning(f"⚠️ sendRichMessage (текст) вернул {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.warning(f"⚠️ Ошибка сети при sendRichMessage (текст): {e}")

    # Если Rich Message совсем сломался, отправляем классическим текстовым сообщением
    await send_fallback_formatted_text(
        context=context,
        chat_id=chat_id,
        post_data=post_data,
        direct_url=direct_url,
        partial_warning=partial_warning,
        reply_to_message_id=reply_to_message_id
    )
    return True, False


async def send_fallback_formatted_text(
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: str | int,
        post_data: dict,
        direct_url: str,
        partial_warning: bool = False,
        reply_to_message_id: int | None = None
):
    """
    Резервный метод. Если Rich Message не работает, формирует обычное
    текстовое сообщение с использованием MarkdownV2/HTML форматирования.
    """
    tags_html = post_data.get("tags_html", "")
    comments = post_data.get("comments", [])
    flow_blocks = post_data.get("flow_blocks", [])

    paragraphs = []
    for b in flow_blocks:
        if b["kind"] == "heading":
            paragraphs.append(b.get("fallback_html", b["html"]))
        elif b["kind"] == "text":
            paragraphs.append(b["html"])

    link_html = f'<a href="{html.escape(direct_url)}">Пост на JoyReactor</a>'

    bottom_parts = []
    if tags_html:
        bottom_parts.append(tags_html)
    if comments:
        joined_comments = "\n\n".join(comments)
        bottom_parts.append(f"💬 <b>Отличные комментарии:</b>\n<blockquote expandable>{joined_comments}</blockquote>")
    if partial_warning:
        bottom_parts.append("<i>Не все медиафайлы поместились, полный пост доступен по ссылке ниже.</i>")
    bottom_parts.append(link_html)

    bottom_block = "\n\n".join(bottom_parts)
    available_body_len = max(800, LIMIT_TEXT_MSG - len(bottom_block) - 120)

    body_block = ""
    if paragraphs:
        preview_list = []
        expand_list = []
        used_len = 0
        is_truncated = False

        for p in paragraphs:
            if used_len + len(p) + 2 > available_body_len:
                rem = available_body_len - used_len - 50
                if rem > 100:
                    expand_list.append(p[:rem].rsplit(" ", 1)[0] + "...")
                is_truncated = True
                break

            if not expand_list and (used_len == 0 or used_len + len(p) <= PREVIEW_MAX_CHARS):
                preview_list.append(p)
            else:
                expand_list.append(p)
            used_len += len(p) + 2

        preview_text = "\n\n".join(preview_list)
        if expand_list:
            expand_text = "\n\n".join(expand_list)
            if is_truncated:
                expand_text += "\n\n<i>... (текст сокращен, продолжение в оригинале поста)</i>"
            if preview_text:
                body_block = f"{preview_text}\n\n<blockquote expandable>{expand_text}</blockquote>"
            else:
                body_block = f"<blockquote expandable>{expand_text}</blockquote>"
        else:
            body_block = preview_text

    final_message = f"{body_block}\n\n{bottom_block}".strip() if body_block else bottom_block

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=final_message,
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_to_message_id=reply_to_message_id
        )
    except Exception as e:
        logger.error(f"Fallback text send error: {e}")


async def _process_media_group(
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: str | int,
        post_id: str,
        media_objects: list,
        related_items: list,
        caption: str | None = None
) -> tuple[bool, int | None]:
    """
    Отправляет массив файлов как "Медиагруппу" (карусель/альбом в Telegram).
    Так как Telegram поддерживает не более 10 файлов за раз, функция разбивает их на чанки по 10.
    """
    caption_sent_here = False
    first_msg_id = None

    while media_objects:
        chunk = media_objects[:MAX_MEDIA_PER_GROUP]
        chunk_items = related_items[:MAX_MEDIA_PER_GROUP]
        is_last_chunk = len(media_objects) <= MAX_MEDIA_PER_GROUP

        # Добавляем подпись (капшен) только к последнему блоку
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
                await asyncio.sleep(3) # Пауза перед отправкой следующей десятки файлов

        except Exception as e:
            # Логика обработки ошибок: проверяем, нужно ли перекачивать файл или попробовать позже
            url_err = is_url_fetch_error(e)
            net_retry = is_retryable_network_error(e)

            if isinstance(e, TimedOut) and all(it["send"] == "err" for it in chunk_items):
                logger.warning(f"Post {post_id}: TimedOut при выгрузке локальных файлов. Помечаем отправленным.")
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
    """
    Главная бизнес-логика отправки поста.
    Сначала пытается отправить его красивым Rich Message.
    Если там есть iframe или Telegram сбоит — отправляет медиа группами, а затем текст отдельным реплаем.
    """
    raw_content_list = post_data.get("content", [])

    truncated_by_limit = len(raw_content_list) > MAX_RICH_MEDIA
    content_list = raw_content_list[:MAX_RICH_MEDIA] if truncated_by_limit else raw_content_list

    # Если в посте ЕСТЬ видео-хостинги (YouTube, Vimeo и т.д.), мы не можем использовать Rich Message
    # для всего поста, так как Rich Message ломает превью плеера.
    has_video_hosting = any(item["type"] == "video_hosting" for item in content_list)

    if not has_video_hosting:
        ok, sent_inline = await send_rich_message_to_telegram(
            context=context,
            chat_id=chat_id,
            post_data=post_data,
            direct_url=f"https://m.joyreactor.cc/post/{post_id}",
            include_media_inline=True,
            partial_warning=truncated_by_limit
        )
        if ok and (sent_inline or not content_list):
            return

    # Логика классической отправки файлов (через send_media_group / send_animation)
    tags_html = post_data.get("tags_html", "")
    flow_blocks = post_data.get("flow_blocks", [])
    comments = post_data.get("comments", [])
    direct_url = f"https://m.joyreactor.cc/post/{post_id}"
    link_post = f'<a href="{direct_url}">Пост на JoyReactor</a>'

    text_blocks = [b for b in flow_blocks if b["kind"] in ("heading", "text")]
    has_text_or_comments = bool(text_blocks or comments or truncated_by_limit)

    post_caption = None
    if not has_text_or_comments:
        post_caption = f"{tags_html}\n\n{link_post}" if tags_html else link_post

    caption_sent = not bool(post_caption)
    everything_sent = not truncated_by_limit
    first_media_msg_id = None

    try:
        while True:
            pending_items = [c for c in content_list if c["send"] not in ("yes", "close")]
            if not pending_items:
                break

            download_retry_occurred = False
            mixed_media_group, mixed_media_items = [], []
            gif_group, gif_items = [], []
            video_hosting_group = []

            # Распределение файлов по типам для правильной отправки
            for content in content_list:
                if content["send"] == "yes":
                    continue
                if content["send"] == "close":
                    everything_sent = False
                    continue

                media_type = content["type"]

                # Если Telegram не смог стянуть по URL ("err"), качаем на свой сервер и отправляем локально
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

                # Подготовка фото
                if media_type == "photo":
                    if content["send"] == "not":
                        mixed_media_group.append(InputMediaPhoto(media=content["data"], parse_mode="HTML"))
                        mixed_media_items.append(content)
                    elif content["send"] == "err" and content.get("local_files"):
                        for f_path in content["local_files"]:
                            if os.path.exists(f_path):
                                with open(f_path, "rb") as f:
                                    mixed_media_group.append(InputMediaPhoto(media=f.read(), parse_mode="HTML"))
                                    mixed_media_items.append(content)

                # Подготовка видео
                elif media_type == "video":
                    if content["send"] == "not":
                        mixed_media_group.append(InputMediaVideo(media=content["data"], parse_mode="HTML"))
                        mixed_media_items.append(content)
                    elif content["send"] == "err" and content.get("local_files"):
                        for f_path in content["local_files"]:
                            if os.path.exists(f_path):
                                with open(f_path, "rb") as f:
                                    mixed_media_group.append(InputMediaVideo(media=f.read(), parse_mode="HTML"))
                                    mixed_media_items.append(content)

                # Подготовка GIF (отправляются отдельно, не в медиагруппе)
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

                # Ссылки на YouTube/Vimeo (отправляются текстом для генерации превью)
                elif media_type == "video_hosting":
                    video_hosting_group.append(content)

            if len(mixed_media_group) > MAX_RICH_MEDIA:
                mixed_media_group = mixed_media_group[:MAX_RICH_MEDIA]
                mixed_media_items = mixed_media_items[:MAX_RICH_MEDIA]
                everything_sent = False

            last_media_type = None
            if video_hosting_group:
                last_media_type = "video_hosting"
            elif gif_group:
                last_media_type = "gif"
            elif mixed_media_group:
                last_media_type = "mixed"

            # Отправка собранных фото и видео (в альбоме)
            if mixed_media_group:
                sent, msg_id = await _process_media_group(
                    context, chat_id, post_id, mixed_media_group, mixed_media_items,
                    caption=post_caption if last_media_type == "mixed" and not caption_sent else None
                )
                if sent:
                    caption_sent = True
                if msg_id and first_media_msg_id is None:
                    first_media_msg_id = msg_id

            # Отправка GIF (через send_animation)
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
                            logger.warning(
                                f"Post {post_id}: TimedOut при отправке локального gif, помечаем отправленным.")
                            item["send"] = "yes"
                            continue
                        url_err = is_url_fetch_error(e)
                        net_retry = is_retryable_network_error(e)
                        if item["send"] == "not":
                            item["send"] = "err" if (url_err or net_retry) else "close"
                        elif item["send"] == "err":
                            send_retries = item.get("send_retries", 0) + 1
                            item["send_retries"] = send_retries
                            if not net_retry or send_retries >= MAX_SEND_RETRIES:
                                item["send"] = "close"
                        if net_retry:
                            await asyncio.sleep(10)

            # Отправка видеохостингов ссылками с превью
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
                            except Exception:
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

        # Отправка оставшегося текста (комменты, длинный текст), отвечая на первое медиа
        if has_text_or_comments or not everything_sent or not caption_sent:
            await send_rich_message_to_telegram(
                context=context,
                chat_id=chat_id,
                post_data=post_data,
                direct_url=direct_url,
                include_media_inline=False,
                partial_warning=(not everything_sent),
                reply_to_message_id=first_media_msg_id
            )

    finally:
        # Очистка временной папки с локально скачанными файлами
        await clear_data_folder()


async def fetch_html(url: str) -> str:
    """Асинхронно скачивает HTML-код страницы сайта."""
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
    """
    Функция-воркер (запускается по расписанию).
    Периодически опрашивает сайты, ищет новые посты, парсит их и вызывает функцию отправки в Telegram.
    """
    global recorded_sent_posts
    post_id = "0"
    has_new_posts = False

    try:
        for url in URLS:
            html_text = await fetch_html(url)
            if not html_text:
                continue

            # Определение чата, в который пойдет пост
            chat_id = TELEGRAM_CHAT_V if url in URLS_V else TELEGRAM_CHAT_PL
            soup = BeautifulSoup(html_text, "html.parser")
            # Ищем все блоки с постами
            posts = soup.find_all("div", class_=lambda c: c and ("postContainer" in c or "content" in c.split()))

            for post in posts:
                # Получаем ID поста для защиты от дублирования
                post_id_full = post.get("id")
                if not post_id_full or "postContainer" not in post_id_full:
                    footer_link = post.find("div", class_="post-footer")
                    a_post = footer_link.find("a", href=lambda h: h and "/post/" in h) if footer_link else None
                    if a_post:
                        post_id = a_post["href"].split("/post/")[-1].split("#")[0].strip("/")
                    else:
                        continue
                else:
                    post_id = post_id_full.split("postContainer")[-1].strip('"')

                # Если пост новый (не найден в истории) - обрабатываем его
                if post_id not in processed_posts[url]:
                    processed_posts[url].append(post_id)
                    has_new_posts = True
                    recorded_sent_posts = True

                    # Запускаем парсинг
                    post_data = parse_joy_post(post)
                    # Если есть хоть какой-то контент — отправляем в Telegram
                    if post_data and (post_data["content"] or post_data["flow_blocks"] or post_data["comments"]):
                        try:
                            await send_post(context, chat_id=chat_id, post_id=post_id, post_data=post_data)
                        except Exception as send_err:
                            logger.error(f"Unhandled error sending post {post_id}: {send_err}")

                        # Сохраняем прогресс и делаем паузу, чтобы не спамить в Telegram
                        await save_sent_posts(context.application)
                        recorded_sent_posts = False
                        await asyncio.sleep(5)

        # Сохранение после прохода всех URL
        if not has_new_posts and recorded_sent_posts:
            await save_sent_posts(context.application)
            recorded_sent_posts = False

    except Exception as e:
        logger.error(f"Error in monitor_website (last post {post_id}): {e}")


# --- Точка входа в приложение (запуск скрипта) ---
if __name__ == "__main__":
    # Настраиваем защиту от флуда в Telegram (Rate Limiter)
    rate_limiter = AIORateLimiter(
        overall_max_rate=30,
        overall_time_period=1,
        group_time_period=60,
        max_retries=3
    )

    # Инициализация ядра бота (Application Builder)
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(request)
        .rate_limiter(rate_limiter)
        .post_init(load_sent_posts)     # Загружаем базу при старте
        .post_shutdown(save_sent_posts) # Сохраняем базу при выключении
        .build()
    )

    # Добавляем задачу мониторинга сайтов в Job Queue (каждые 60 секунд)
    application.job_queue.run_repeating(monitor_website, interval=60, first=1)

    logger.info("Bot started via PTB Application")
    # Запуск бесконечного цикла (прослушка команд и выполнение заданий)
    application.run_polling(drop_pending_updates=True)