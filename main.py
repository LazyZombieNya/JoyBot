#Ишем все <span> и если есть закрытие </span> то делаем перенос строки
#<div class="image">  src= ссылка на картинку  title= теги
#<div class="image zoomed-image" класс где картинку можно увеличить <a href= ссылка на картинку <img alt= теги
#< a href =  ссылка между <span> стоит текст ссылки
#<div class="single"> контент с видео
#    < video data - src = ссылка на видео
#    title= теги

from bs4 import BeautifulSoup
import requests

def parse_joyreactor():
    # URL сайта
    url = "https://joyreactor.cc/"

    # Отправляем GET-запрос
    response = requests.get(url)
    response.raise_for_status()  # Проверка на ошибки

    # Парсим содержимое страницы
    soup = BeautifulSoup(response.text, 'html.parser')

    # Находим все посты
    posts = soup.find_all('div', class_='post-content')
    print(posts)

    # Обрабатываем каждый пост
    parsed_posts = []
    for post in posts:
        post_content = []

        processed_images = set()  # Для отслеживания уже обработанных изображений по имени файла

        # Работаем с <div class="image zoomed-image">
        for zoomed_img_div in post.find_all('div', class_='image zoomed-image'):
            a_tag = zoomed_img_div.find('a')
            if a_tag and a_tag.get('href'):
                img_url = a_tag['href']
                img_name = img_url.split('/')[-1]  # Получаем имя файла
                if img_name not in processed_images:
                    post_content.append({
                        'type': 'zoomed_image',
                        'url': img_url,
                        'tags': a_tag.find('img').get('alt', 'Нет тегов') if a_tag.find('img') else 'Нет тегов'
                    })
                    processed_images.add(img_name)  # Помечаем имя файла как обработанное

        # Работаем с <div class="image">
        for img_div in post.find_all('div', class_='image'):
            img_tag = img_div.find('img')
            if img_tag and img_tag.get('src'):
                img_url = img_tag['src']
                img_name = img_url.split('/')[-1]  # Получаем имя файла
                if img_name not in processed_images:  # Проверяем, обработано ли изображение
                    post_content.append({
                        'type': 'image',
                        'url': img_url,
                        'tags': img_tag.get('title', 'Нет тегов')
                    })
                    processed_images.add(img_name)  # Помечаем имя файла как обработанное


        # Работаем с <a> (ссылки)
        for a_tag in post.find_all('a'):
            span_text = a_tag.find('span').get_text(strip=True) if a_tag.find('span') else None
            if span_text:
                post_content.append({
                    'type': 'link',
                    'url': a_tag['href'],
                    'text': span_text
                })

        # Работаем с видео
        for video_div in post.find_all('div', class_='ant-spin-nested-loading'):
            video_tag = video_div.find('video')
            if video_tag and video_tag.get('data-src'):
                post_content.append({
                    'type': 'video',
                    'url': video_tag['data-src'],
                    'tags': video_tag.get('title', 'Нет тегов')
                })

        for single_div in post.find_all('div', class_='single'):
            video_tag = single_div.find('video')
            if video_tag and video_tag.get('data-src'):
                post_content.append({
                    'type': 'video',
                    'url': video_tag['data-src'],
                    'tags': video_tag.get('title', 'Нет тегов')
                })

        # Работаем с текстом (<span>)
        for span_tag in post.find_all('span'):
            span_text = span_tag.get_text(strip=True)
            if span_text:
                post_content.append({
                    'type': 'text',
                    'content': span_text
                })

        parsed_posts.append(post_content)

    return parsed_posts

# Выводим результаты
posts_data = parse_joyreactor()
for i, post in enumerate(posts_data, start=1):
    print(f"Пост {i}:")
    for content in post:
        if content['type'] == 'image':
            print(f"Картинка: {content['url']} (Теги: {content['tags']})")
        elif content['type'] == 'zoomed_image':
            print(f"Увеличиваемая картинка: {content['url']} (Теги: {content['tags']})")
        elif content['type'] == 'link':
            print(f"Ссылка: {content['url']} (Текст: {content['text']})")
        elif content['type'] == 'video':
            print(f"Видео: {content['url']} (Теги: {content['tags']})")
        elif content['type'] == 'text':
            print(f"Текст: {content['content']}")
    print("-" * 40)

print(posts_data)