import os
import logging
import sqlite3
import httpx
import smtplib
import urllib.parse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from typing import List
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка базового логирования
logging.basicConfig(level=logging.INFO)

app = FastAPI()

# Настройка CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Инициализация базы данных ---
def init_db():
    conn = sqlite3.connect("orders.db")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            phone TEXT,
            comment TEXT,
            filename TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# Создаем папку для локальных загрузок
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# --- Фоновая задача 1: Отправка Email ---
def send_order_email(order_id: int, name: str, phone: str, comment: str, format: str = "Не указан", paper: str = "Не указана", crop: str = "Не указано"):
    smtp_server = os.getenv("SMTP_SERVER")
    smtp_port = int(os.getenv("SMTP_PORT", 465))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    email_to = os.getenv("EMAIL_TO")

    if not all([smtp_server, smtp_user, smtp_password, email_to]):
        logging.error("Не все настройки SMTP заданы в .env")
        return

    msg = MIMEMultipart()
    msg["From"] = smtp_user
    msg["To"] = email_to
    msg["Subject"] = f"Новый заказ #{order_id} | PhotoDoc AI"
    
    safe_folder_name = f"Заказ_{order_id}_{name.replace(' ', '_')}"
    raw_path = f"PhotoDoc_Orders/{safe_folder_name}"
    safe_url_path = urllib.parse.quote(raw_path)
    yandex_disk_link = f"https://disk.yandex.ru/client/disk/{safe_url_path}"
    
    body = f"Новый заказ!\nНомер: {order_id}\nИмя: {name}\nТелефон: {phone}\nФормат: {format}\nБумага: {paper}\nКадрирование: {crop}\nКомментарий: {comment}\n\nСсылка на Яндекс.Диск: {yandex_disk_link}"
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        logging.info(f"Email для заказа {order_id} успешно отправлен.")
    except Exception as e:
        logging.error(f"Ошибка при отправке Email: {e}")

# --- Вспомогательная функция для Яндекс.Диска ---
def ensure_yandex_folder(token: str, path: str):
    import httpx
    headers = {"Authorization": f"OAuth {token}"}
    parts = [p for p in path.strip("/").split("/") if p]
    current_path = ""
    for part in parts:
        current_path = f"{current_path}/{part}" if current_path else part
        try:
            httpx.put(
                "https://cloud-api.yandex.net/v1/disk/resources",
                headers=headers,
                params={"path": current_path},
                timeout=10
            )
        except Exception:
            pass # Игнорируем ошибки (например, 409 если папка уже есть)

# --- Фоновая задача 2: Загрузка на Яндекс.Диск ---
def upload_to_yandex_disk(local_file_path: str, remote_folder_path: str, remote_file_name: str):
    import os, logging
    import httpx

    token = os.getenv("YANDEX_DISK_TOKEN")
    if not token:
        logging.error("YANDEX_DISK_TOKEN не задан в .env")
        return

    ensure_yandex_folder(token, remote_folder_path)

    headers = {"Authorization": f"OAuth {token}"}
    remote_path = f"{remote_folder_path}/{remote_file_name}"

    try:
        # Шаг 1: получаем URL для загрузки
        # Примечание: API Яндекса требует, чтобы родительская папка существовала.
        # Если папки PhotoDoc_Orders нет, сервер может вернуть ошибку 409. 
        # В таком случае просто создай её один раз руками в вебе Я.Диска.
        upload_url_response = httpx.get(
            "https://cloud-api.yandex.net/v1/disk/resources/upload",
            headers=headers,
            params={"path": remote_path, "overwrite": "true"},
            timeout=30
        )
        upload_url_response.raise_for_status()
        href = upload_url_response.json().get("href")

        if not href:
            logging.error(f"Яндекс.Диск не вернул href: {upload_url_response.text}")
            return

        # Шаг 2: загружаем файл по полученному URL
        with open(local_file_path, "rb") as f:
            put_response = httpx.put(href, content=f.read(), timeout=60)
            put_response.raise_for_status()

        logging.info(f"Файл {remote_file_name} успешно загружен на Яндекс.Диск в папку PhotoDoc_Orders/")

    except httpx.HTTPStatusError as e:
        logging.error(f"Ошибка HTTP при загрузке на Яндекс.Диск: {e.response.status_code} — {e.response.text}")
    except Exception as e:
        logging.error(f"Неизвестная ошибка при загрузке на Яндекс.Диск: {e}")

# --- Фоновая задача 3: Загрузка инфо-файла на Яндекс.Диск ---
def upload_info_to_yandex_disk(order_id: int, name: str, phone: str, format: str, paper: str, crop: str, comment: str, remote_folder_path: str):
    import os, logging
    import httpx

    token = os.getenv("YANDEX_DISK_TOKEN")
    if not token:
        return

    ensure_yandex_folder(token, remote_folder_path)

    order_info_text = f"""Заказ №: {order_id}
Имя: {name}
Телефон: {phone}
Формат: {format}
Бумага: {paper}
Кадрирование: {crop}
Комментарий: {comment}"""

    headers = {"Authorization": f"OAuth {token}"}
    info_file_path = f"{remote_folder_path}/info.txt"

    try:
        res = httpx.get(
            "https://cloud-api.yandex.net/v1/disk/resources/upload",
            headers=headers,
            params={"path": info_file_path, "overwrite": "true"},
            timeout=30
        )
        if res.status_code == 200:
            upload_url = res.json().get("href")
            if upload_url:
                httpx.put(upload_url, content=order_info_text.encode('utf-8'), timeout=30)
                logging.info(f"Файл info.txt успешно загружен на Яндекс.Диск (Заказ {order_id})")
        else:
            logging.error(f"Яндекс.Диск вернул ошибку при получении ссылки для info.txt: {res.text}")
    except Exception as e:
        logging.error(f"Ошибка при загрузке info.txt на Яндекс.Диск: {e}")

# --- Эндпоинт приема заказа ---
@app.post("/api/order")
async def create_order(
    background_tasks: BackgroundTasks,
    name: str = Form(...),
    phone: str = Form(...),
    comment: str = Form(""),
    format: str = Form("Не указан"),
    paper: str = Form("Не указана"),
    crop: str = Form("Не указано"),
    files: List[UploadFile] = File(...)
):
    try:
        # 1. Сохраняем данные в SQLite для получения номера заказа
        filenames = ", ".join([f.filename for f in files])
        conn = sqlite3.connect("orders.db")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO orders (name, phone, comment, filename) VALUES (?, ?, ?, ?)",
            (name, phone, comment, filenames)
        )
        conn.commit()
        order_id = cursor.lastrowid
        conn.close()

        # 2. Формируем имена для локального сохранения и облака
        safe_name = name.replace(" ", "_")
        safe_settings = f"{format}_{paper}_{crop}".replace(" ", "_")
        remote_folder_path = f"PhotoDoc_Orders/Заказ_{order_id}_{safe_name}/{safe_settings}"

        for file in files:
            safe_filename = file.filename.replace(" ", "_")
            local_filename = f"{order_id}_{safe_filename}"
            local_file_path = os.path.join(UPLOAD_DIR, local_filename)
            
            # 3. Сохраняем физический файл на жесткий диск
            with open(local_file_path, "wb") as f:
                content = await file.read()
                f.write(content)

            # 4. Формируем имя файла для Яндекс.Диска
            remote_file_name = f"{order_id}_{safe_filename}"

            # 5. Передаем работу в фоновые задачи (для каждого файла)
            background_tasks.add_task(upload_to_yandex_disk, local_file_path, remote_folder_path, remote_file_name)
            
        # Фоновая задача для Email выполняется один раз на весь заказ
        background_tasks.add_task(send_order_email, order_id, name, phone, comment, format, paper, crop)

        # Фоновая задача для текстового файла с деталями заказа
        background_tasks.add_task(upload_info_to_yandex_disk, order_id, name, phone, format, paper, crop, comment, remote_folder_path)

        # 6. Моментально отвечаем фронтенду
        return {
            "ok": True, 
            "message": "Заказ успешно создан", 
            "order_id": order_id
        }

    except Exception as e:
        logging.error(f"Критическая ошибка при создании заказа: {e}")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")