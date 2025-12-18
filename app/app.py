import os
import sqlite3
import subprocess # Нужен для запуска FFmpeg
import requests
import re
from flask import Flask, render_template, request, redirect, url_for, g, jsonify

app = Flask(__name__)
DB_PATH = '/app/data/veterans.db'
COLAB_URL = "https://nonintoxicating-maynard-superobediently.ngrok-free.dev"
# Папка, куда сохраняем картинки
THUMBNAIL_FOLDER = '/app/static/thumbnails'
os.makedirs(THUMBNAIL_FOLDER, exist_ok=True)

def get_clean_colab_url():
    """Удаляет невидимые пробелы и мусор из ссылки"""
    if not COLAB_URL: return ""
    # Оставляем только английские буквы, цифры, точки, двоеточия, слэши и дефисы
    clean = re.sub(r'[^\w\d:\/\.\-]', '', COLAB_URL)
    return clean

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    if not os.path.exists(DB_PATH):
        print("Создаем базу данных...")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            person_name TEXT,
            category TEXT,
            type TEXT NOT NULL,
            path TEXT NOT NULL,
            thumbnail_path TEXT,
            transcript TEXT,
            colab_task_id TEXT, -- НОВОЕ ПОЛЕ: Номер задачи в Colab
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    c.execute('''
        CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts 
        USING fts5(title, transcript, person_name, content='videos', content_rowid='id')
    ''')
    
    c.execute('CREATE TRIGGER IF NOT EXISTS videos_ai AFTER INSERT ON videos BEGIN INSERT INTO videos_fts(rowid, title, transcript, person_name) VALUES (new.id, new.title, new.transcript, new.person_name); END;')
    conn.commit()
    conn.close()

init_db()

# --- ФУНКЦИЯ ГЕНЕРАЦИИ ОБЛОЖКИ ---
def generate_thumbnail(video_type, video_path, video_id):
    """
    Пытается создать картинку с помощью FFmpeg.
    Возвращает имя файла (например 'thumb_12.jpg') или None.
    """
    try:
        output_filename = f"thumb_{video_id}.jpg"
        output_path = os.path.join(THUMBNAIL_FOLDER, output_filename)
        
        # Определяем входной файл для FFmpeg
        input_source = None
        
        if video_type == 'local':
            input_source = os.path.join('/app/static/videos', video_path)
        elif video_type == 'direct':
            input_source = video_path # FFmpeg умеет читать URL
        
        # Если источник понятен, запускаем FFmpeg
        if input_source:
            # Команда: взять кадр на 00:00:02
            cmd = [
                'ffmpeg', '-y', 
                '-ss', '00:00:02', 
                '-i', input_source, 
                '-vframes', '1', 
                '-q:v', '2', 
                output_path
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return output_filename
            
    except Exception as e:
        print(f"Не удалось создать превью: {e}")
        return None
    
    return None

@app.route('/')
def index():
    query = request.args.get('q', '')
    category = request.args.get('category', 'all')
    conn = get_db()
    
    sql = "SELECT * FROM videos WHERE 1=1"
    params = []

    if category != 'all':
        sql += " AND category = ?"
        params.append(category)

    if query:
        search_sql = f"""
            SELECT videos.*, snippet(videos_fts, 1, '<mark class="bg-yellow-200">', '</mark>', '...', 10) as snippet 
            FROM videos 
            JOIN videos_fts ON videos.id = videos_fts.rowid 
            WHERE videos_fts MATCH ?
        """
        if category != 'all': search_sql += " AND category = ?"
        search_sql += " ORDER BY rank"
        search_params = [f"{query}*"]
        if category != 'all': search_params.append(category)
        videos = conn.execute(search_sql, search_params).fetchall()
    else:
        sql += " ORDER BY created_at DESC"
        videos = conn.execute(sql, params).fetchall()
    
    return render_template('index.html', videos=videos, search_query=query, current_category=category)


@app.route('/video/<int:video_id>')
def video_page(video_id):
    conn = get_db()
    video_row = conn.execute('SELECT * FROM videos WHERE id = ?', (video_id,)).fetchone()
    
    if not video_row:
        return "Видео не найдено", 404

    video_data = dict(video_row)
    
    # ПРОВЕРКА ОБНОВЛЕНИЯ (Обернута в защиту, чтобы не было Error 500)
    try:
        current_text = video_data.get('transcript')
        task_id = video_data.get('colab_task_id')
        
        # Если текста нет, но есть задача
        if not current_text and task_id and COLAB_URL:
            
            # --- ЯДЕРНАЯ ЧИСТКА ССЫЛКИ ---
            # 1. Удаляем все невидимые символы через кодировку
            # Это удалит вообще всё, что не является стандартным текстом
            clean_url = COLAB_URL.encode('ascii', 'ignore').decode('ascii').strip()
            # 2. Убираем слэш в конце, если есть
            clean_url = clean_url.rstrip('/')
            
            full_link = f"{clean_url}/api/status/{task_id}"
            print(f"[DEBUG] ЧИСТАЯ ССЫЛКА: {full_link}")
            
            # Запрос (тайм-аут 60 сек, без SSL)
            response = requests.get(full_link, timeout=60, verify=False)
            
            if response.status_code == 200:
                result = response.json()
                status = result.get('status')
                
                if status == 'done':
                    new_text = result.get('text')
                    # Обновляем БД
                    conn.execute('UPDATE videos SET transcript = ? WHERE id = ?', (new_text, video_id))
                    conn.commit()
                    video_data['transcript'] = new_text
                    print("✅ ТЕКСТ ПОЛУЧЕН!")
                elif status == 'error':
                    err = f"Ошибка Colab: {result.get('text')}"
                    conn.execute('UPDATE videos SET transcript = ? WHERE id = ?', (err, video_id))
                    conn.commit()
                    video_data['transcript'] = err
            else:
                print(f"Colab ответил кодом: {response.status_code}")

    except Exception as e:
        # Если случилась ошибка, мы не роняем сайт, а пишем её в консоль
        print(f"⚠️ ОШИБКА ПРИ ОБНОВЛЕНИИ: {e}")
        # Можно даже вывести её на экран временно, чтобы ты увидел:
        # return f"ОШИБКА: {e}", 500 

    conn.close()
    return render_template('video.html', video=video_data)

@app.route('/delete/<int:video_id>', methods=['POST'])
def delete_video(video_id):
    conn = get_db()
    conn.execute('DELETE FROM videos WHERE id = ?', (video_id,))
    conn.commit()
    return redirect(url_for('index'))

@app.route('/add', methods=['GET', 'POST'])
def add_video():
    if request.method == 'POST':
        print("\n--- НАЧИНАЮ ДОБАВЛЕНИЕ ВИДЕО ---") # ЛОГ
        
        title = request.form['title']
        person_name = request.form['person_name']
        category = request.form['category']
        v_type = request.form['type']
        path = request.form['path']
        manual_thumb = request.form.get('manual_thumbnail', '').strip()
        
        # ЛОГ
        print(f"Тип видео: {v_type}")
        print(f"Адрес Colab: {COLAB_URL}")

        conn = get_db()
        cursor = conn.cursor()
        
        # 1. Сохраняем в базу
        cursor.execute('''
            INSERT INTO videos (title, person_name, category, type, path, transcript, thumbnail_path, colab_task_id) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (title, person_name, category, v_type, path, '', manual_thumb, None))
        
        new_video_id = cursor.lastrowid
        print(f"Видео сохранено в БД под ID: {new_video_id}") # ЛОГ
        
        # 2. Отправка в Colab
        if v_type != 'local':
            if COLAB_URL:
                print(f"Попытка отправки запроса на {COLAB_URL}/api/task ...") # ЛОГ
                try:
                    payload = {"url": path}
                    response = requests.post(f"{COLAB_URL}/api/task", json=payload, timeout=10)
                    
                    print(f"Код ответа Colab: {response.status_code}") # ЛОГ
                    print(f"Тело ответа: {response.text}") # ЛОГ

                    if response.status_code == 200:
                        data = response.json()
                        task_id = data.get('task_id')
                        cursor.execute('UPDATE videos SET colab_task_id = ? WHERE id = ?', (task_id, new_video_id))
                        print(f"УСПЕХ! ID задачи: {task_id}")
                    else:
                        print("ОШИБКА: Colab ответил не 200")
                        
                except Exception as e:
                    print(f"КРИТИЧЕСКАЯ ОШИБКА СОЕДИНЕНИЯ: {e}")
            else:
                print("ПРОПУСК: Переменная COLAB_URL пустая!")
        else:
            print("ПРОПУСК: Видео локальное, Colab не нужен.")

        conn.commit()
        conn.close()
        
        print("--- ЗАВЕРШЕНО ---\n")
        return redirect(url_for('index'))
        
    return render_template('add.html')
# --- API ДЛЯ GOOGLE COLAB ---

@app.route('/api/upload', methods=['POST'])
def api_upload():
    try:
        data = request.json
        
        # Получаем данные от Colab
        title = data.get('title', 'Без названия (из Colab)')
        person_name = data.get('person_name', 'Не указано')
        category = data.get('category', 'other')
        v_type = data.get('type', 'youtube') # youtube или direct
        path = data.get('path', '')
        transcript = data.get('transcript', '')
        
        # Генерируем тумбнейл, если это прямая ссылка
        thumbnail_path = None
        # (Тут можно вызвать generate_thumbnail, но пока оставим пустым, чтобы не усложнять API)

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO videos (title, person_name, category, type, path, transcript, thumbnail_path) 
            VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (title, person_name, category, v_type, path, transcript, thumbnail_path))
        conn.commit()
        conn.close()
        
        return jsonify({"status": "success", "message": "Видео успешно добавлено!"}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)