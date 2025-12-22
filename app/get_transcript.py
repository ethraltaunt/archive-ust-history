import os
import re
import requests
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.formatters import TextFormatter

# --- НАСТРОЙКИ ---
INPUT_FILE = 'links.txt'
OUTPUT_DIR = 'transcripts' # Куда сохранять тексты

def get_video_id(url):
    """Вытаскивает ID из ссылки YouTube"""
    # Ищем v=... или просто ID в коротких ссылках
    match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11}).*", url)
    return match.group(1) if match else None

def get_video_title(video_id):
    """Получает название видео (простой способ без API ключа)"""
    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        response = requests.get(url)
        # Ищем тег <title> в HTML
        matches = re.findall(r'<title>(.*?)</title>', response.text)
        if matches:
            # Убираем " - YouTube" из названия
            return matches[0].replace(" - YouTube", "").strip()
    except:
        pass
    return f"Video_{video_id}"

def clean_filename(title):
    """Убираем плохие символы из названия файла"""
    return "".join([c for c in title if c.isalpha() or c.isdigit() or c in ' -_']).strip()

def main():
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

    if not os.path.exists(INPUT_FILE):
        print(f"❌ Файл {INPUT_FILE} не найден!")
        return

    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        urls = [line.strip() for line in f if line.strip()]

    formatter = TextFormatter()
    print(f"Найдено {len(urls)} ссылок. Начинаю скачивание текстов...\n")

    for i, url in enumerate(urls, 1):
        video_id = get_video_id(url)
        
        if not video_id:
            print(f"[{i}] ⚠️ Некорректная ссылка: {url}")
            continue

        print(f"[{i}] Обработка ID: {video_id}...")
        
        try:
            # 1. Получаем название видео (для красивого файла)
            title = get_video_title(video_id)
            safe_filename = clean_filename(title)
            
            # 2. Скачиваем транскрипцию (пробуем русский)
            # languages=['ru'] ищет русские сабы. Если нет, можно добавить 'en'
            transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['ru'])
            
            # 3. Превращаем в чистый текст
            text_formatted = formatter.format_transcript(transcript_list)
            
            # 4. Сохраняем
            file_path = f"{OUTPUT_DIR}/{safe_filename}.txt"
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(f"Ссылка: {url}\n")
                f.write(f"Название: {title}\n")
                f.write("-" * 30 + "\n\n")
                f.write(text_formatted)
                
            print(f"✅ Успешно! Файл: {safe_filename}.txt")

        except Exception as e:
            # Частая ошибка - субтитры отключены
            if "TranscriptsDisabled" in str(e):
                print(f"❌ Ошибка: У этого видео отключены субтитры.")
            elif "NoTranscriptFound" in str(e):
                print(f"❌ Ошибка: Русские субтитры не найдены.")
            else:
                print(f"❌ Ошибка: {e}")
        
        print("-" * 30)

if __name__ == '__main__':
    main()