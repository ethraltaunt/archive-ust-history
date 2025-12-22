import os
import sqlite3
import subprocess # Нужен для запуска FFmpeg
import requests
import re
from flask import Flask, render_template, request, redirect, url_for, g, jsonify, session, flash
from functools import wraps 


app = Flask(__name__)
DB_PATH = '/app/data/veterans.db'
COLAB_URL = "https://nonintoxicating-maynard-superobediently.ngrok-free.dev"
MY_SITE_PUBLIC_URL = 'localhost'
THUMBNAIL_FOLDER = '/app/static/thumbnails'
os.makedirs(THUMBNAIL_FOLDER, exist_ok=True)
ADMIN_PASSWORD = "admin"
app.secret_key = 'super_secret_key_change_me' 

# --- НОВЫЙ WEBHOOK: Colab сам пришлет сюда текст ---
@app.route('/api/callback', methods=['POST'])
def receive_transcript():
    try:
        # 1. Получаем данные
        data = request.json
        # Мы передадим video_id (наш ID в базе), чтобы точно знать, кого обновлять
        video_id = data.get('video_id') 
        transcript = data.get('text')
        status = data.get('status')
        
        print(f"\n[WEBHOOK] Получен сигнал от Colab для видео {video_id}, статус: {status}")

        if not video_id:
            return jsonify({"error": "No video_id provided"}), 400

        conn = get_db()
        
        if status == 'done' and transcript:
            # Сохраняем текст в базу
            conn.execute('UPDATE videos SET transcript = ? WHERE id = ?', (transcript, video_id))
            conn.commit()
            print(f"[WEBHOOK] ✅ Видео {video_id}: Текст сохранен в базу!")
            
        elif status == 'error':
            error_msg = f"Ошибка обработки: {data.get('error_msg')}"
            conn.execute('UPDATE videos SET transcript = ? WHERE id = ?', (error_msg, video_id))
            conn.commit()
            print(f"[WEBHOOK] ❌ Видео {video_id}: Получена ошибка.")

        conn.close()
        return jsonify({"message": "OK, data received"}), 200

    except Exception as e:
        print(f"[WEBHOOK ERROR] {e}")
        return jsonify({"error": str(e)}), 500

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

# --- 4. РОУТЫ ВХОДА И ВЫХОДА ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form['password'] == ADMIN_PASSWORD:
            session['logged_in'] = True
            # Если пользователь хотел попасть на конкретную страницу, вернем его туда
            next_url = request.args.get('next')
            return redirect(next_url or url_for('index'))
        else:
            error = 'Неверный пароль'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('index'))

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
            transcript TEXT,
            thumbnail_path TEXT,
            colab_task_id TEXT,
            source_name TEXT,  -- НОВОЕ ПОЛЕ: Красивое название источника
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # ... (остальной код FTS и триггеров без изменений) ...
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
    video = conn.execute('SELECT * FROM videos WHERE id = ?', (video_id,)).fetchone()
    conn.close()
    
    if not video:
        return "Видео не найдено", 404
        
    # Никаких requests! Просто отдаем то, что есть.
    return render_template('video.html', video=video)

@app.route('/delete/<int:video_id>', methods=['POST'])
@login_required
def delete_video(video_id):
    conn = get_db()
    conn.execute('DELETE FROM videos WHERE id = ?', (video_id,))
    conn.commit()
    return redirect(url_for('index'))

@app.route('/add', methods=['GET', 'POST'])
@login_required
def add_video():
    if request.method == 'POST':
        title = request.form['title']
        person_name = request.form['person_name']
        category = request.form['category']
        v_type = request.form['type']
        path = request.form['path']
        transcript = request.form['transcript'].strip() # Убираем пробелы по краям
        manual_thumb = request.form.get('manual_thumbnail', '').strip()
        source_name = request.form['source_name'] # Получаем красивое имя источника
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Сохраняем видео. task_id пока None.
        cursor.execute('''
            INSERT INTO videos (title, person_name, category, type, path, transcript, thumbnail_path, colab_task_id, source_name) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (title, person_name, category, v_type, path, transcript, manual_thumb, None, source_name))
        
        new_video_id = cursor.lastrowid
        
        # ЛОГИКА: Отправляем в Colab ТОЛЬКО если текст пустой И это не локальный файл
        if not transcript and v_type != 'local' and COLAB_URL:
            try:
                # Адрес для Webhook (если используешь Push-режим)
                # MY_SITE_PUBLIC_URL = "https://твоя-ссылка.ngrok-free.app" 
                
                print(f"Отправка задачи в Colab для видео {new_video_id}...")
                
                # Если используешь простой режим (как мы отладили последним):
                response = requests.post(
                    f"{COLAB_URL}/api/task", 
                    json={"url": path}, 
                    timeout=5
                )
                
                if response.status_code == 200:
                    data = response.json()
                    task_id = data.get('task_id')
                    cursor.execute('UPDATE videos SET colab_task_id = ? WHERE id = ?', (task_id, new_video_id))
                    print(f"Задача ушла в Colab. ID: {task_id}")
            except Exception as e:
                print(f"Ошибка связи с Colab: {e}")
        else:
            print("Colab пропущен (либо есть текст, либо локальное видео).")

        conn.commit()
        conn.close()
        
        return redirect(url_for('index'))
        
    return render_template('add.html')

@app.route('/api/upload', methods=['POST'])
@login_required
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