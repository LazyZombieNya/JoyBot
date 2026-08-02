import asyncio
import os
import shutil
import json
import platform
from io import BytesIO
from urllib.parse import urlparse

import aiohttp
import aiofiles
import ffmpeg
from PIL import Image
import math

# Ограничения Telegram
MAX_SIZE_IMG_MB = 10  # Максимальный размер фото в MB
MAX_SIZE_VIDEO_MB = 50  # Максимальный размер видео в MB
MAX_IMAGE_SIDE = 4096  # Максимальная длина одной стороны картинки
MAX_IMAGE_SIDES_SUM = 10000  # Telegram: сумма width+height не должна превышать это значение
MAX_IMAGE_RATIO = 19  # Telegram режет фото с соотношением сторон > 20:1, берём 19 с запасом

MAX_VIDEO_COMPRESS_ATTEMPTS = 4  # Сколько раз пробуем пересжать видео, если не влезло в лимит
MIN_VIDEO_BITRATE = 80_000  # Нижний предел битрейта видео (bps), ниже — совсем не смотрибельно

DATA_FOLDER = "temp_data"  # Папка где хранятся временно скачанные файлы
BASE_DIR = os.path.dirname(os.path.abspath(__file__)) # Место положение скрипта

# Отключаем защиту Pillow от "DecompressionBomb", чтобы открывать огромные файлы
Image.MAX_IMAGE_PIXELS = None

# FFmpeg мультимедийный фреймворк для работы с медиафайлами
if platform.system() == "Windows":
    FFMPEG_PATH = os.path.join(BASE_DIR, "lib", "ffmpeg.exe")
    FFPROBE_PATH = os.path.join(BASE_DIR, "lib", "ffprobe.exe")

    if not os.path.exists(FFMPEG_PATH):
        raise FileNotFoundError(
            f"FFmpeg not found at path {FFMPEG_PATH}"
        )

    if not os.path.exists(FFPROBE_PATH):
        raise FileNotFoundError(
            f"FFprobe not found at path {FFPROBE_PATH}"
        )

else:
    if shutil.which("ffmpeg") is None:
        raise FileNotFoundError(
            "FFmpeg not found in PATH. Install it: sudo apt install ffmpeg -y"
        )

    if shutil.which("ffprobe") is None:
        raise FileNotFoundError(
            "FFprobe not found in PATH. Install it: sudo apt install ffmpeg -y"
        )

    FFMPEG_PATH = "ffmpeg"
    FFPROBE_PATH = "ffprobe"

# Узнаем какого разрешения файл по ссылке
def get_file_extension(url):
    parsed_url = urlparse(url)
    path = parsed_url.path.strip('/')  # Достаем путь из ссылки и убираем лишние слэши
    parts = path.rsplit('.', 1)  # Разделяем по последней точке на части

    if len(parts) == 2 and parts[1]:  # Если есть расширение (т.е. состоит из 2 частей) возвращаем в нижнем регистре
        return parts[1].lower()
    return ""  # Если расширения нет, возвращаем пустую строку

# Возвращает размер файла
def format_size(size_bytes):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024

# получение информации о видео
async def get_video_info(path):
    command = [
        FFPROBE_PATH,
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        path
    ]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, _ = await process.communicate()
    data = json.loads(stdout)

    video_stream = next(
        s for s in data["streams"]
        if s["codec_type"] == "video"
    )

    return {
        "width": int(video_stream["width"]),
        "height": int(video_stream["height"]),
        "duration": float(data["format"]["duration"]),
        "bitrate": int(data["format"].get("bit_rate", 0))
    }

# Удаляет все файлы в папке DATA_FOLDER, если они есть.
async def clear_data_folder():
    if os.path.exists(DATA_FOLDER):
        for file in os.listdir(DATA_FOLDER):
            file_path = os.path.join(DATA_FOLDER, file)
            try:
                os.remove(file_path)
            except Exception as e:
                print(f"Error deleting {file_path}: {e}")

def _fit_telegram_photo_dimensions(img):
    width, height = img.size

    if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
        ratio = min(MAX_IMAGE_SIDE / width, MAX_IMAGE_SIDE / height)
        width, height = max(1, int(width * ratio)), max(1, int(height * ratio))
        img = img.resize((width, height), Image.LANCZOS)

    long_side, short_side = max(width, height), min(width, height)
    if short_side > 0 and long_side / short_side > MAX_IMAGE_RATIO:
        target_long = max(1, int(short_side * MAX_IMAGE_RATIO))
        if width >= height:
            left = (width - target_long) // 2
            img = img.crop((left, 0, left + target_long, height))
            width = target_long
        else:
            top = (height - target_long) // 2
            img = img.crop((0, top, width, top + target_long))
            height = target_long

    if width + height > MAX_IMAGE_SIDES_SUM:
        ratio = MAX_IMAGE_SIDES_SUM / (width + height)
        width, height = max(1, int(width * ratio)), max(1, int(height * ratio))
        img = img.resize((width, height), Image.LANCZOS)

    return img

"""
    Разрезает длинное изображение на равные части по вертикали.
    Режет только "вытянутые" картинки (комиксы/свитки).
    """
def split_long_image(image_path, max_height=4000):
    img = Image.open(image_path)
    width, height = img.size

    # КОЭФФИЦИЕНТ ВЫТЯНУТОСТИ:
    # Делим высоту на ширину.
    # Если картинка шире своей высоты (пейзаж) или почти квадратная,
    # коэффициент будет меньше 1.5. Мы такие картинки не режем!
    # Они пойдут в функцию compress_image и аккуратно сожмутся.
    if height <= max_height or (height / width) < 1.5:
        return [image_path]

    # Вычисляем количество частей и высоту каждой из них
    num_parts = math.ceil(height / max_height)
    part_height = math.ceil(height / num_parts)

    parts = []
    base_dir = os.path.dirname(image_path)
    base_name, ext = os.path.splitext(os.path.basename(image_path))

    for i in range(num_parts):
        top = i * part_height
        # Убеждаемся, что нижняя граница не выходит за пределы картинки
        bottom = min((i + 1) * part_height, height)

        box = (0, top, width, bottom)
        cropped_img = img.crop(box)

        if cropped_img.mode in ("RGBA", "P") and ext.lower() in (".jpg", ".jpeg"):
            cropped_img = cropped_img.convert("RGB")

        part_path = os.path.join(base_dir, f"{base_name}_part_{i}{ext}")
        cropped_img.save(part_path)
        parts.append(part_path)

    return parts

#Сжатие картинок если они больше MAX_SIZE_IMG_MB
async def compress_image(image_path_or_bytes, max_size=MAX_SIZE_IMG_MB * 1024 * 1024):
    if isinstance(image_path_or_bytes, bytes):
        img = Image.open(BytesIO(image_path_or_bytes))
    else:
        img = Image.open(image_path_or_bytes)

    img = img.convert("RGB")
    img = _fit_telegram_photo_dimensions(img)

    quality = 90
    while True:
        output = BytesIO()
        img.save(output, format="JPEG", quality=quality, optimize=True)
        size = output.tell()

        if size <= max_size:
            return output.getvalue()
        if quality > 50:
            quality -= 5
            continue

        width, height = img.size
        if width < 1000 or height < 1000:
            return output.getvalue()
        img = img.resize((int(width * 0.9), int(height * 0.9)), Image.LANCZOS)

def _pick_scale(video_bitrate):
    if video_bitrate >= 2_500_000:
        return 1280
    elif video_bitrate >= 1_200_000:
        return 720
    elif video_bitrate >= 700_000:
        return 640
    elif video_bitrate >= 450_000:
        return 480
    elif video_bitrate >= 250_000:
        return 360
    else:
        return 256

async def _encode_video(input_path, temp_output, video_bitrate, audio_bitrate, scale):
    command = [
        FFMPEG_PATH,
        "-y",
        "-i", input_path,
        "-vf",
        f"scale='min(iw,{scale})':-2",
        "-c:v", "libx264",
        "-preset", "fast",
        "-b:v", str(video_bitrate),
        "-maxrate", str(int(video_bitrate * 1.2)),
        "-bufsize", str(int(video_bitrate * 2)),
        "-c:a", "aac",
        "-b:a", f"{audio_bitrate // 1000}k",
        "-movflags", "+faststart",
        temp_output
    ]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        print("FFmpeg command:")
        print(" ".join(command))
        print(stderr.decode(errors="ignore"))
        return False

    return True

#Сжатие видео с помощью FFMPEG. Если после прохода файл всё ещё не влезает
#в лимит — пересчитываем битрейт по фактическому результату и повторяем.
async def compress_video(input_path, output_path):
    info = await get_video_info(input_path)

    duration = info["duration"]
    width = info["width"]
    height = info["height"]

    size_mb = os.path.getsize(input_path) / (1024 * 1024)

    print(
        f"Video info: "
        f"{width}x{height}, "
        f"{duration:.1f}s, "
        f"{size_mb:.2f} MB"
    )

    target_size_mb = MAX_SIZE_VIDEO_MB * 0.92

    target_total_bitrate = int(
        target_size_mb * 8 * 1024 * 1024 / duration
    )

    if duration > 1800:
        audio_bitrate = 64_000
    elif duration > 600:
        audio_bitrate = 96_000
    else:
        audio_bitrate = 128_000

    # Раньше здесь стоял жёсткий пол в 300 kbps, который на длинных видео
    # (20+ минут) был в разы больше реально нужного битрейта и не позволял
    # уложиться в лимит ни при каких условиях. Теперь пол ниже, а нехватку
    # размера ловим ниже через повторные попытки с коррекцией битрейта.
    video_bitrate = max(
        MIN_VIDEO_BITRATE,
        target_total_bitrate - audio_bitrate
    )

    temp_output = output_path + ".tmp.mp4"

    for attempt in range(1, MAX_VIDEO_COMPRESS_ATTEMPTS + 1):
        scale = _pick_scale(video_bitrate)

        print(
            f"Attempt {attempt}/{MAX_VIDEO_COMPRESS_ATTEMPTS}: "
            f"target video bitrate {video_bitrate / 1000:.0f} kbps, "
            f"audio {audio_bitrate / 1000:.0f} kbps, scale {scale}p"
        )

        ok = await _encode_video(input_path, temp_output, video_bitrate, audio_bitrate, scale)
        if not ok:
            if os.path.exists(temp_output):
                os.remove(temp_output)
            return False

        new_size_mb = os.path.getsize(temp_output) / (1024 * 1024)

        print(
            f"Compressed (attempt {attempt}): "
            f"{size_mb:.2f} MB -> "
            f"{new_size_mb:.2f} MB"
        )

        if new_size_mb <= MAX_SIZE_VIDEO_MB:
            if os.path.exists(output_path):
                os.remove(output_path)
            shutil.move(temp_output, output_path)
            return True

        # Не влезли — считаем, во сколько раз фактический результат больше
        # цели, и пропорционально режем битрейт с запасом 10%, вместо того
        # чтобы гадать заново с фиксированным шагом.
        achieved_total_bitrate = (new_size_mb * 8 * 1024 * 1024) / duration
        os.remove(temp_output)

        if achieved_total_bitrate <= 0:
            break

        correction = (target_total_bitrate / achieved_total_bitrate) * 0.9
        new_video_bitrate = max(MIN_VIDEO_BITRATE, int(video_bitrate * correction))

        if new_video_bitrate >= video_bitrate:
            break  # коррекция ничего не меняет — дальше пытаться бессмысленно

        video_bitrate = new_video_bitrate

    print(
        f"Failed to compress {input_path} below {MAX_SIZE_VIDEO_MB} MB "
        f"after {MAX_VIDEO_COMPRESS_ATTEMPTS} attempts, size {format_size(os.path.getsize(input_path))}"
    )
    return False

#Функция конвертации Gif в Mp4
async def gif_to_mp4(input_path, output_path):
    ffmpeg.input(input_path).output(
        output_path, vcodec="libx264", crf=28, preset="fast"
    ).run(overwrite_output=True)
    return output_path

#Скачивает медиафайлы на диск со сжатием
async def download_media(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/122.0.0.0",
        "Referer": url,
    }

    ext = get_file_extension(url)
    name_only = os.path.splitext(url.split("/")[-1])[0].lower()
    filename = f"temp_{name_only[:40]}.{ext}"

    os.makedirs(DATA_FOLDER, exist_ok=True)
    file_path = os.path.join(DATA_FOLDER, filename)

    # total=900: максимум 15 минут на полное скачивание файла
    # sock_read=45: если сервер не присылает ни одного байта данных в течение 45 секунд — рвем соединение
    timeout = aiohttp.ClientTimeout(total=900, sock_read=45, sock_connect=30)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:

                    temp_path = file_path + "_temp"
                    # Потоковое скачивание (chunks), чтобы не ловить TimeoutError на чтении всего файла в память
                    async with aiofiles.open(temp_path, 'wb') as f:
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            await f.write(chunk)

                    size_file = os.path.getsize(temp_path)
                    print(f"Downloaded {temp_path} size {format_size(size_file)}")


                    # ПРОВЕРКА НА 0 КБ: Если файл пустой или загрузка оборвалась — прерываем процесс
                    if not os.path.exists(temp_path) or size_file == 0:
                        print(f"Ошибка: Скачанный файл пустой или соединение разорвано: {url}")
                        if os.path.exists(temp_path): os.remove(temp_path)
                        return None

                    if ext in ['jpeg', 'png', 'bmp', 'webp']:
                        shutil.move(temp_path, file_path)
                        # Разрезаем файл (если он длинный)
                        image_parts = split_long_image(file_path, max_height=4000)

                        final_paths = []
                        for i, part in enumerate(image_parts):
                            # Сжимаем каждый кусок стандартной функцией
                            compressed_bytes = await compress_image(part)
                            part_final_path = f"{file_path}_part{i}.{ext}" if len(image_parts) > 1 else file_path

                            async with aiofiles.open(part_final_path, 'wb') as img_file:
                                await img_file.write(compressed_bytes)

                            final_paths.append(part_final_path)

                            # Удаляем сырой нарезанный кусок, чтобы не забивать диск
                            if part != file_path:
                                os.remove(part)

                        # Удаляем оригинальный целый файл, если он был разрезан на части
                        if len(image_parts) > 1 and os.path.exists(file_path):
                            os.remove(file_path)

                        return final_paths  # Возвращаем список путей

                    elif ext in ['mp4', 'avi', 'mov', 'mkv', 'webm', 'gif']:
                        if ext == "gif":
                            compressed_path = file_path.replace(".gif", ".mp4")
                            await gif_to_mp4(temp_path, compressed_path)
                            file_path = compressed_path
                        elif size_file < MAX_SIZE_VIDEO_MB * 1024 * 1024:
                            shutil.move(temp_path, file_path)
                        else:
                            success = await compress_video(temp_path, file_path)
                            if not success:
                                print(f"Failed to compress {temp_path} below {MAX_SIZE_VIDEO_MB} MB, size {format_size(size_file)}")
                                # os.remove(temp_path)
                                return None
                            if os.path.exists(temp_path):
                                os.remove(temp_path)
                    else:
                        os.remove(temp_path)
                        return None

                    return [file_path]
                else:
                    print(f"Ошибка HTTP: {response.status} для {url}")
    except asyncio.TimeoutError:
        print(f"Timeout while downloading: {url}")
        return None
    except Exception as e:
        print(f"Error processing {url}: {e}")
        return None