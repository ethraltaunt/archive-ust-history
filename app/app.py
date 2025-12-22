import os
import sqlite3
import subprocess
import requests
import re
from flask import Flask, render_template, request, redirect, url_for, g, jsonify, session, flash
from functools import wraps 
import shutil

app = Flask(__name__)

# --- КОНФИГУРАЦИЯ ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__)) # Папка /app
DB_PATH = os.path.join(BASE_DIR, 'data', 'veterans.db') # /app/data/veterans.db

# Папки для медиа
VIDEOS_DIR = os.path.join(BASE_DIR, 'static', 'videos')
THUMBNAIL_FOLDER = os.path.join(BASE_DIR, 'static', 'thumbnails')

# Создаем папки, если нет
os.makedirs(VIDEOS_DIR, exist_ok=True)
os.makedirs(THUMBNAIL_FOLDER, exist_ok=True)

# ССЫЛКИ
# Ссылка на Google Colab (из ngrok)
COLAB_URL = "https://nonintoxicating-maynard-superobediently.ngrok-free.dev"

# ВАЖНО: Это адрес ТВОЕГО сайта, который виден из интернета (для Colab).
# Если ты на локалке -> вставь сюда свой Ngrok (http://xxxx.ngrok.io)
# Если на сервере -> вставь IP или домен (http://1.2.3.4:5000)
# 'localhost' работать НЕ будет, так как Colab не знает, где твой localhost.
MY_SITE_PUBLIC_URL = "https://твоя-ссылка-на-этот-сайт.ngrok-free.app" 

ADMIN_PASSWORD = "admin"
app.secret_key = 'super_secret_key_change_me' 


# --- БАЗА ДАННЫХ ---
def get_db():
    # Создаем папку data если её нет (для первого запуска)
    db_dir = os.path.dirname(DB_PATH)
    if not os.path.exists(db_dir):
        os.makedirs(db_dir)
        
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
    conn = get_db() # Используем get_db для надежности
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
            source_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts 
        USING fts5(title, transcript, person_name, content='videos', content_rowid='id')
    ''')
    c.execute('CREATE TRIGGER IF NOT EXISTS videos_ai AFTER INSERT ON videos BEGIN INSERT INTO videos_fts(rowid, title, transcript, person_name) VALUES (new.id, new.title, new.transcript, new.person_name); END;')
    conn.commit()

# Инициализация при старте
with app.app_context():
    init_db()


# --- ФУНКЦИИ ПОМОЩНИКИ ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def generate_thumbnail(video_type, video_path, video_id):
    """Генерирует обложку с помощью FFmpeg"""
    print(f"\n--- ГЕНЕРАЦИЯ ОБЛОЖКИ (ID: {video_id}) ---")
    
    input_file = None
    if video_type == 'local':
        input_file = os.path.join(VIDEOS_DIR, video_path)
    elif video_type == 'direct':
        input_file = video_path
    else:
        return None

    if video_type == 'local' and not os.path.exists(input_file):
        print(f"ОШИБКА: Видео файл не найден: {input_file}")
        return None

    thumb_filename = f"thumb_{video_id}.jpg"
    output_file = os.path.join(THUMBNAIL_FOLDER, thumb_filename)

    cmd = [
        'ffmpeg', '-y', 
        '-ss', '00:00:05', # Кадр на 5-й секунде
        '-i', input_file, 
        '-vframes', '1', 
        '-q:v', '2', 
        output_file
    ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        print(f"УСПЕХ: Обложка создана -> {thumb_filename}")
        return thumb_filename
    except Exception as e:
        print(f"ОШИБКА FFmpeg: {e}")
        return None


# --- WEBHOOK (Принимаем данные от Colab) ---
@app.route('/api/callback', methods=['POST'])
def receive_transcript():
    try:
        data = request.json
        video_id = data.get('video_id') 
        transcript = data.get('text')
        status = data.get('status')
        
        print(f"\n[WEBHOOK] Сигнал от Colab для видео {video_id}, статус: {status}")

        if not video_id: return jsonify({"error": "No video_id"}), 400

        conn = get_db()
        if status == 'done' and transcript:
            conn.execute('UPDATE videos SET transcript = ? WHERE id = ?', (transcript, video_id))
            conn.commit()
            print(f"✅ Видео {video_id}: Текст сохранен!")
        elif status == 'error':
            error_msg = f"Ошибка обработки: {data.get('error_msg')}"
            conn.execute('UPDATE videos SET transcript = ? WHERE id = ?', (error_msg, video_id))
            conn.commit()
            print(f"❌ Видео {video_id}: Ошибка.")

        conn.close()
        return jsonify({"message": "OK"}), 200
    except Exception as e:
        print(f"[WEBHOOK ERROR] {e}")
        return jsonify({"error": str(e)}), 500


# --- РОУТЫ ИНТЕРФЕЙСА ---

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
    if not video: return "Видео не найдено", 404
    return render_template('video.html', video=video)


@app.route('/add', methods=['GET', 'POST'])
@login_required
def add_video():
    if request.method == 'POST':
        title = request.form['title']
        person_name = request.form['person_name']
        category = request.form['category']
        v_type = request.form['type']
        path = request.form['path']
        transcript = request.form['transcript'].strip()
        manual_thumb = request.form.get('manual_thumbnail', '').strip()
        source_name = request.form['source_name']
        
        conn = get_db()
        cursor = conn.cursor()
        
        # 1. Сохраняем основную запись
        cursor.execute('''
            INSERT INTO videos (title, person_name, category, type, path, transcript, thumbnail_path, colab_task_id, source_name) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (title, person_name, category, v_type, path, transcript, manual_thumb, None, source_name))
        
        new_id = cursor.lastrowid
        conn.commit() # Важно закоммитить, чтобы видео получило ID

        # 2. ГЕНЕРАЦИЯ ОБЛОЖКИ (Если нет ручной)
        final_thumb = manual_thumb
        if not final_thumb and v_type in ['local', 'direct']:
            generated = generate_thumbnail(v_type, path, new_id)
            if generated:
                cursor.execute('UPDATE videos SET thumbnail_path = ? WHERE id = ?', (generated, new_id))
                conn.commit()

        # 3. ОТПРАВКА В COLAB (Если нет текста и это не локальное видео)
        if not transcript and v_type != 'local' and COLAB_URL:
            try:
                # Отправляем задачу в режиме Webhook (чтобы Colab сам вернул ответ)
                task_payload = {
                    "url": path,
                    "video_id": new_id, # Наш ID
                    "callback_url": f"{MY_SITE_PUBLIC_URL}/api/callback" # Куда стучаться обратно
                }
                
                print(f"Отправка задачи в Colab...")
                requests.post(f"{COLAB_URL}/api/task", json=task_payload, timeout=2)
                
            except Exception as e:
                print(f"Не удалось связаться с Colab: {e}")

        conn.close()
        return redirect(url_for('index'))
        
    return render_template('add.html')


@app.route('/delete/<int:video_id>', methods=['POST'])
@login_required
def delete_video(video_id):
    conn = get_db()
    conn.execute('DELETE FROM videos WHERE id = ?', (video_id,))
    conn.commit()
    return redirect(url_for('index'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form['password'] == ADMIN_PASSWORD:
            session['logged_in'] = True
            next_url = request.args.get('next')
            return redirect(next_url or url_for('index'))
        else:
            error = 'Неверный пароль'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('index'))

# --- СЛУЖЕБНЫЙ РОУТ ДЛЯ ПОЧИНКИ КАРТИНОК ---
@app.route('/fix_thumbs')
@login_required
def fix_thumbs():
    conn = get_db()
    videos = conn.execute("SELECT * FROM videos WHERE type='local' AND (thumbnail_path IS NULL OR thumbnail_path = '')").fetchall()
    count = 0
    for v in videos:
        thumb = generate_thumbnail('local', v['path'], v['id'])
        if thumb:
            conn.execute('UPDATE videos SET thumbnail_path = ? WHERE id = ?', (thumb, v['id']))
            count += 1
    conn.commit()
    conn.close()
    return f"Сгенерировано обложек: {count}. <a href='/'>На главную</a>"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)