import os
import logging
import sqlite3
import httpx
import smtplib
import urllib.parse
import hashlib
from decimal import Decimal, InvalidOperation
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from typing import List
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from pydantic import BaseModel

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
            filename TEXT,
            status TEXT DEFAULT 'new',
            payment_status TEXT DEFAULT 'unpaid',
            payment_amount TEXT,
            payment_method TEXT,
            robokassa_inv_id TEXT,
            paid_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()


def ensure_orders_schema():
    conn = sqlite3.connect("orders.db")
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(orders)")
    columns = {row[1] for row in cursor.fetchall()}
    extra_columns = {
        "status": "TEXT DEFAULT 'new'",
        "payment_status": "TEXT DEFAULT 'unpaid'",
        "payment_amount": "TEXT",
        "payment_method": "TEXT",
        "robokassa_inv_id": "TEXT",
        "paid_at": "TEXT",
    }
    for column_name, column_sql in extra_columns.items():
        if column_name not in columns:
            cursor.execute(f"ALTER TABLE orders ADD COLUMN {column_name} {column_sql}")
    conn.commit()
    conn.close()


ensure_orders_schema()

# Создаем папку для локальных загрузок
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


def format_robokassa_amount(value: float | int | str) -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid amount")

    normalized = amount.normalize()
    amount_str = format(normalized, "f")
    if "." in amount_str:
        amount_str = amount_str.rstrip("0").rstrip(".")
    return amount_str or "0"


def update_order_payment(order_id: int, payment_amount: str, payment_status: str, payment_method: str = "robokassa"):
    conn = sqlite3.connect("orders.db")
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE orders
        SET payment_amount = ?, payment_status = ?, payment_method = ?, status = ?, robokassa_inv_id = ?, paid_at = ?
        WHERE id = ?
        """,
        (
            payment_amount,
            payment_status,
            payment_method,
            "paid" if payment_status == "paid" else "awaiting_payment",
            str(order_id),
            datetime.utcnow().isoformat() if payment_status == "paid" else None,
            order_id,
        ),
    )
    conn.commit()
    updated = cursor.rowcount
    conn.close()
    return updated


def get_order_payment_amount(order_id: int):
    conn = sqlite3.connect("orders.db")
    cursor = conn.cursor()
    cursor.execute("SELECT payment_amount FROM orders WHERE id = ?", (order_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


class PaymentCreateRequest(BaseModel):
    order_id: int | str
    amount: float | int


@app.post("/api/payment/create")
async def create_payment(payment: PaymentCreateRequest):
    merchant_login = os.getenv("ROBOKASSA_MERCHANT_LOGIN")
    password1 = os.getenv("ROBOKASSA_PASSWORD1")
    is_test = os.getenv("ROBOKASSA_IS_TEST", "1")

    if not merchant_login or not password1:
        raise HTTPException(status_code=500, detail="Robokassa settings are not configured")

    try:
        order_id_int = int(str(payment.order_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="order_id must be an integer")

    amount = format_robokassa_amount(payment.amount)
    updated = update_order_payment(order_id_int, amount, "pending")
    if not updated:
        raise HTTPException(status_code=404, detail="Order not found")

    order_id = str(order_id_int)
    signature = hashlib.md5(
        f"{merchant_login}:{amount}:{order_id}:{password1}".encode("utf-8")
    ).hexdigest()
    params = {
        "MerchantLogin": merchant_login,
        "OutSum": amount,
        "InvId": order_id,
        "Description": f"Оплата_заказа_{order_id}",
        "SignatureValue": signature,
        "IsTest": is_test,
    }
    payment_url = "https://auth.robokassa.ru/Merchant/Index.aspx?" + urllib.parse.urlencode(params)
    return {"payment_url": payment_url}


def verify_robokassa_signature(out_sum: str, inv_id: str, signature: str, password: str) -> bool:
    expected = hashlib.md5(f"{out_sum}:{inv_id}:{password}".encode("utf-8")).hexdigest()
    return expected.lower() == signature.lower()


async def read_robokassa_payload(request: Request):
    payload = dict(request.query_params)
    if request.method.upper() == "POST":
        form_data = await request.form()
        payload.update(dict(form_data))
    return payload


@app.api_route("/api/payment/robokassa/result", methods=["GET", "POST"])
async def robokassa_result(request: Request):
    password2 = os.getenv("ROBOKASSA_PASSWORD2")
    if not password2:
        raise HTTPException(status_code=500, detail="Robokassa password2 is not configured")

    payload = await read_robokassa_payload(request)
    out_sum = payload.get("OutSum")
    inv_id = payload.get("InvId")
    signature = payload.get("SignatureValue")

    if not out_sum or inv_id is None or not signature:
        raise HTTPException(status_code=400, detail="Missing Robokassa payload")

    try:
        order_id = int(str(inv_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid order id")

    if not verify_robokassa_signature(str(out_sum), str(inv_id), str(signature), password2):
        raise HTTPException(status_code=400, detail="Invalid signature")

    stored_amount = get_order_payment_amount(order_id)
    expected_amount = format_robokassa_amount(stored_amount or out_sum)
    if format_robokassa_amount(out_sum) != expected_amount:
        raise HTTPException(status_code=400, detail="Amount mismatch")

    updated = update_order_payment(order_id, expected_amount, "paid")
    if not updated:
        raise HTTPException(status_code=404, detail="Order not found")

    return PlainTextResponse(f"OK{order_id}")


@app.api_route("/api/payment/robokassa/success", methods=["GET", "POST"])
async def robokassa_success(request: Request):
    payload = await read_robokassa_payload(request)
    return JSONResponse(
        {
            "ok": True,
            "order_id": payload.get("InvId"),
            "message": "Платеж подтвержден",
        }
    )

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
