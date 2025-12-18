FROM python:3.10-slim

WORKDIR /app

# На всякий случай оставляем ffmpeg (вдруг захочешь генерировать превью картинки)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .
RUN mkdir -p /app/data

# Запуск через Gunicorn (профессиональный веб-сервер)
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "--timeout", "300", "app:app"]