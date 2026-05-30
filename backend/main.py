import os
import logging
import re
import sqlite3
import httpx
import smtplib
import urllib.parse
import hashlib
import html
from pathlib import Path
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from typing import List
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "orders.db"

# Загрузка переменных окружения
load_dotenv(BASE_DIR / ".env")

# Настройка базового логирования
logging.basicConfig(level=logging.INFO)

app = FastAPI()

DEFAULT_CORS_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost",
    "http://127.0.0.1",
    "https://photodoc-ai.ru",
    "https://www.photodoc-ai.ru",
)


def get_cors_origins():
    raw_origins = os.getenv("CORS_ORIGINS", "")
    if not raw_origins.strip():
        return list(DEFAULT_CORS_ORIGINS)

    return [origin.strip() for origin in raw_origins.split(",") if origin.strip()]

# Настройка CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Инициализация базы данных ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            phone TEXT,
            email TEXT,
            format TEXT,
            paper TEXT,
            crop TEXT,
            comment TEXT,
            filename TEXT,
            status TEXT DEFAULT 'new',
            payment_status TEXT DEFAULT 'unpaid',
            payment_amount TEXT,
            payment_method TEXT,
            robokassa_inv_id TEXT,
            paid_at TEXT,
            payment_email_sent_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()


def ensure_orders_schema():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(orders)")
    columns = {row[1] for row in cursor.fetchall()}
    extra_columns = {
        "status": "TEXT DEFAULT 'new'",
        "email": "TEXT",
        "format": "TEXT",
        "paper": "TEXT",
        "crop": "TEXT",
        "payment_status": "TEXT DEFAULT 'unpaid'",
        "payment_amount": "TEXT",
        "payment_method": "TEXT",
        "robokassa_inv_id": "TEXT",
        "paid_at": "TEXT",
        "payment_email_sent_at": "TEXT",
    }
    for column_name, column_sql in extra_columns.items():
        if column_name not in columns:
            cursor.execute(f"ALTER TABLE orders ADD COLUMN {column_name} {column_sql}")
    conn.commit()
    conn.close()


ensure_orders_schema()

# Создаем папку для локальных загрузок
UPLOAD_DIR = BASE_DIR / "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


def sanitize_upload_filename(filename: str | None) -> str:
    base_name = (filename or "upload").replace("\\", "/").split("/")[-1]
    safe_name = re.sub(r"[^A-Za-zА-Яа-яЁё0-9._-]+", "_", base_name).strip("._")
    return safe_name or "upload"


def format_robokassa_amount(value: float | int | str) -> str:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid amount")

    return format(amount, ".2f")


def update_order_payment(order_id: int, payment_amount: str, payment_status: str, payment_method: str = "robokassa"):
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT payment_amount FROM orders WHERE id = ?", (order_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def get_order_for_email(order_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, name, phone, email, format, paper, crop, comment, filename, payment_email_sent_at
        FROM orders
        WHERE id = ?
        """,
        (order_id,),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def mark_order_payment_email_sent(order_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE orders SET payment_email_sent_at = ? WHERE id = ?",
        (datetime.utcnow().isoformat(), order_id),
    )
    conn.commit()
    conn.close()


def send_client_paid_email_and_mark(order_id: int, order: dict):
    if order.get("payment_email_sent_at"):
        logging.info(f"Клиентский Email для оплаченного заказа {order_id} уже отправлялся, повтор пропущен.")
        return

    sent = send_client_paid_email(
        order_id,
        order.get("name") or "",
        order.get("email") or "",
    )
    if sent:
        mark_order_payment_email_sent(order_id)


def schedule_paid_order_email(background_tasks: BackgroundTasks, order_id: int):
    order = get_order_for_email(order_id)
    if not order:
        logging.error(f"Заказ {order_id} не найден для отправки Email после оплаты.")
        return

    background_tasks.add_task(send_client_paid_email_and_mark, order_id, order)


class PaymentCreateRequest(BaseModel):
    order_id: int | str
    amount: float | int
    email: str | None = None


@app.post("/api/payment/create")
async def create_payment(payment: PaymentCreateRequest):
    merchant_login = os.getenv("ROBOKASSA_MERCHANT_LOGIN")
    password1 = os.getenv("ROBOKASSA_PASSWORD1")
    is_test = os.getenv("ROBOKASSA_IS_TEST", "1")
    is_test_value = "1" if str(is_test).strip().lower() in {"1", "true", "yes", "on"} else "0"

    if not merchant_login or not password1:
        raise HTTPException(status_code=500, detail="Robokassa settings are not configured")

    try:
        order_id_int = int(str(payment.order_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="order_id must be an integer")

    order = get_order_for_email(order_id_int)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    amount = format_robokassa_amount(payment.amount)
    updated = update_order_payment(order_id_int, amount, "pending")
    if not updated:
        raise HTTPException(status_code=404, detail="Order not found")

    order_id = str(order_id_int)
    customer_email = (payment.email or order.get("email") or "").strip()
    signature_source = f"{merchant_login}:{amount}:{order_id}:{password1}"
    signature = hashlib.md5(
        signature_source.encode("utf-8")
    ).hexdigest()
    params = {
        "MerchantLogin": merchant_login,
        "OutSum": amount,
        "InvId": order_id,
        "Description": f"Оплата_заказа_{order_id}",
        "SignatureValue": signature,
        "IsTest": is_test_value,
    }
    if customer_email:
        params["Email"] = customer_email

    payment_url = "https://auth.robokassa.ru/Merchant/Index.aspx?" + urllib.parse.urlencode(params)
    return {"payment_url": payment_url}


def verify_robokassa_signature(out_sum: str, inv_id: str, signature: str, password: str) -> bool:
    expected = hashlib.md5(f"{out_sum}:{inv_id}:{password}".encode("utf-8")).hexdigest()
    return expected.lower() == signature.lower()


def get_robokassa_signature(out_sum: str, inv_id: str, password: str) -> str:
    return hashlib.md5(f"{out_sum}:{inv_id}:{password}".encode("utf-8")).hexdigest()


def log_robokassa_signature_check(endpoint_name: str, out_sum: str, inv_id: str, signature: str, password: str) -> bool:
    expected = get_robokassa_signature(out_sum, inv_id, password)
    is_valid = expected.lower() == (signature or "").lower()
    logging.info(
        "%s: проверка подписи %s. expected=%s received=%s",
        endpoint_name,
        "прошла" if is_valid else "НЕ прошла",
        expected,
        signature,
    )
    return is_valid


async def read_robokassa_payload(request: Request):
    payload = dict(request.query_params)
    if request.method.upper() == "POST":
        form_data = await request.form()
        payload.update(dict(form_data))
    return payload


@app.api_route("/api/payment/robokassa/result", methods=["GET", "POST"])
async def robokassa_result(request: Request, background_tasks: BackgroundTasks):
    logging.info("Получен запрос на Result URL от Робокассы: method=%s", request.method)
    password2 = os.getenv("ROBOKASSA_PASSWORD2")
    if not password2:
        logging.error("Result URL: ROBOKASSA_PASSWORD2 не настроен")
        raise HTTPException(status_code=500, detail="Robokassa password2 is not configured")

    payload = await read_robokassa_payload(request)
    out_sum = payload.get("OutSum")
    inv_id = payload.get("InvId")
    signature = payload.get("SignatureValue")
    logging.info("Result URL: OutSum=%s InvId=%s SignatureValue=%s", out_sum, inv_id, signature)

    if not out_sum or inv_id is None or not signature:
        logging.error("Result URL: не хватает параметров Robokassa payload=%s", payload)
        raise HTTPException(status_code=400, detail="Missing Robokassa payload")

    try:
        order_id = int(str(inv_id))
    except ValueError:
        logging.error("Result URL: некорректный InvId=%s", inv_id)
        raise HTTPException(status_code=400, detail="Invalid order id")

    if not log_robokassa_signature_check("Result URL", str(out_sum), str(inv_id), str(signature), password2):
        raise HTTPException(status_code=400, detail="Invalid signature")

    stored_amount = get_order_payment_amount(order_id)
    expected_amount = format_robokassa_amount(stored_amount or out_sum)
    logging.info("Result URL: order_id=%s stored_amount=%s expected_amount=%s", order_id, stored_amount, expected_amount)
    if format_robokassa_amount(out_sum) != expected_amount:
        logging.error("Result URL: сумма не совпала. received=%s expected=%s", format_robokassa_amount(out_sum), expected_amount)
        raise HTTPException(status_code=400, detail="Amount mismatch")

    updated = update_order_payment(order_id, expected_amount, "paid")
    if not updated:
        logging.error("Result URL: заказ %s не найден для обновления статуса paid", order_id)
        raise HTTPException(status_code=404, detail="Order not found")
    logging.info("Result URL: заказ №%s обновлен на статус paid", order_id)

    schedule_paid_order_email(background_tasks, order_id)
    logging.info("Result URL: задача отправки клиентского письма для заказа №%s поставлена в background_tasks", order_id)

    return PlainTextResponse(f"OK{order_id}")


@app.api_route("/api/payment/robokassa/success", methods=["GET", "POST"])
async def robokassa_success(request: Request, background_tasks: BackgroundTasks):
    logging.info("Получен запрос на Success URL от Робокассы: method=%s", request.method)
    password1 = os.getenv("ROBOKASSA_PASSWORD1")
    if not password1:
        logging.error("Success URL: ROBOKASSA_PASSWORD1 не настроен")
        raise HTTPException(status_code=500, detail="Robokassa password1 is not configured")

    payload = await read_robokassa_payload(request)
    out_sum = payload.get("OutSum")
    inv_id = payload.get("InvId")
    signature = payload.get("SignatureValue")
    order_id = None
    logging.info("Success URL: OutSum=%s InvId=%s SignatureValue=%s", out_sum, inv_id, signature)

    if out_sum and inv_id is not None and signature:
        try:
            order_id = int(str(inv_id))
        except ValueError:
            logging.error("Success URL: некорректный InvId=%s", inv_id)
            raise HTTPException(status_code=400, detail="Invalid order id")

        if not log_robokassa_signature_check("Success URL", str(out_sum), str(inv_id), str(signature), password1):
            raise HTTPException(status_code=400, detail="Invalid signature")

        expected_amount = format_robokassa_amount(get_order_payment_amount(order_id) or out_sum)
        logging.info("Success URL: order_id=%s expected_amount=%s", order_id, expected_amount)
        if format_robokassa_amount(out_sum) != expected_amount:
            logging.error("Success URL: сумма не совпала. received=%s expected=%s", format_robokassa_amount(out_sum), expected_amount)
            raise HTTPException(status_code=400, detail="Amount mismatch")

        updated = update_order_payment(order_id, expected_amount, "paid")
        if not updated:
            logging.error("Success URL: заказ %s не найден для обновления статуса paid", order_id)
            raise HTTPException(status_code=404, detail="Order not found")
        logging.info("Success URL: заказ №%s обновлен на статус paid", order_id)

        schedule_paid_order_email(background_tasks, order_id)
        logging.info("Success URL: задача отправки клиентского письма для заказа №%s поставлена в background_tasks", order_id)
    else:
        logging.warning("Success URL: параметры оплаты пришли не полностью, письмо клиенту не ставилось. payload=%s", payload)

    return JSONResponse(
        {
            "ok": True,
            "order_id": order_id or payload.get("InvId"),
            "message": "Платеж подтвержден",
        }
    )

# --- Фоновая задача 1: Отправка Email ---
def create_email_message(sender: str, recipient: str, subject: str, text_body: str, html_body: str | None = None):
    msg = MIMEMultipart("alternative") if html_body else MIMEMultipart()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg


def build_yandex_disk_link(order_id: int, name: str):
    safe_folder_name = f"Заказ_{order_id}_{(name or '').replace(' ', '_')}"
    raw_path = f"PhotoDoc_Orders/{safe_folder_name}"
    safe_url_path = urllib.parse.quote(raw_path)
    return f"https://disk.yandex.ru/client/disk/{safe_url_path}"


def send_admin_order_email(
    order_id: int,
    name: str,
    phone: str,
    client_email: str = "",
    comment: str = "",
    format: str = "Не указан",
    paper: str = "Не указана",
    crop: str = "Не указано",
    filenames: str = "",
    remote_folder_path: str = "",
):
    smtp_server = os.getenv("SMTP_SERVER")
    smtp_port = int(os.getenv("SMTP_PORT", 465))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    admin_email = os.getenv("EMAIL_TO")
    client_email = (client_email or "").strip()

    if not all([smtp_server, smtp_user, smtp_password, admin_email]):
        logging.error("Не все настройки SMTP заданы в .env")
        return False

    try:
        yandex_disk_link = build_yandex_disk_link(order_id, name)
        admin_body = (
            f"Новый заказ!\n"
            f"Номер: {order_id}\n"
            f"Имя: {name}\n"
            f"Телефон: {phone}\n"
            f"Email клиента: {client_email or 'Не указан'}\n"
            f"Формат: {format}\n"
            f"Бумага: {paper}\n"
            f"Кадрирование: {crop}\n"
            f"Комментарий: {comment}\n"
            f"Файлы: {filenames or 'Не указаны'}\n"
            f"Папка на Яндекс.Диске: {remote_folder_path or 'Будет создана фоновой задачей'}\n\n"
            f"Ссылка на Яндекс.Диск: {yandex_disk_link}"
        )
        admin_msg = create_email_message(
            smtp_user,
            admin_email,
            f"Новый заказ #{order_id} | PhotoDoc AI",
            admin_body,
        )

        with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
            server.login(smtp_user, smtp_password)
            server.send_message(admin_msg)

        logging.info(f"Административный Email для заказа {order_id} успешно отправлен.")
        return True
    except Exception as e:
        logging.error(f"Ошибка при отправке административного Email: {e}")
        return False


def send_client_paid_email(order_id: int, name: str, client_email: str = ""):
    smtp_server = os.getenv("SMTP_SERVER")
    smtp_port = int(os.getenv("SMTP_PORT", 465))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    client_email = (client_email or "").strip()

    if not all([smtp_server, smtp_user, smtp_password]):
        logging.error("Не все настройки SMTP заданы в .env")
        return False

    if not client_email:
        logging.warning(f"Клиентский Email для заказа {order_id} не отправлен: email клиента не указан.")
        return False

    try:
        client_text = (
            f"Здравствуйте, {name or 'клиент'}!\n\n"
            f"Спасибо за заказ в PhotoDoc AI. Ваш заказ №{order_id} успешно оплачен и взят в работу.\n"
            f"Мы сообщим вам о готовности.\n\n"
            f"С уважением,\nPhotoDoc AI"
        )
        safe_client_name = html.escape(name or "клиент")
        client_html = f"""
        <div style="font-family:Arial,sans-serif;color:#111827;line-height:1.6">
          <h2 style="margin:0 0 16px;color:#111827">Спасибо за заказ в PhotoDoc AI!</h2>
          <p>Здравствуйте, {safe_client_name}.</p>
          <p>Ваш заказ <strong>№{order_id}</strong> успешно оплачен и взят в работу.</p>
          <p>Мы сообщим вам о готовности.</p>
          <p style="margin-top:24px;color:#6b7280">С уважением,<br>PhotoDoc AI</p>
        </div>
        """

        with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
            server.login(smtp_user, smtp_password)
            client_msg = create_email_message(
                smtp_user,
                client_email,
                f"Заказ №{order_id} оплачен | PhotoDoc AI",
                client_text,
                client_html,
            )
            server.send_message(client_msg)

        logging.info(f"Клиентский Email для заказа {order_id} успешно отправлен на {client_email}.")
        return True
    except Exception as e:
        logging.error(f"Ошибка при отправке клиентского Email: {e}")
        return False

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
    email: str = Form(""),
    comment: str = Form(""),
    format: str = Form("Не указан"),
    paper: str = Form("Не указана"),
    crop: str = Form("Не указано"),
    files: List[UploadFile] = File(...)
):
    try:
        # 1. Сохраняем данные в SQLite для получения номера заказа
        safe_filenames = [sanitize_upload_filename(f.filename) for f in files]
        filenames = ", ".join(safe_filenames)
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO orders (name, phone, email, format, paper, crop, comment, filename)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, phone, email.strip(), format, paper, crop, comment, filenames)
        )
        conn.commit()
        order_id = cursor.lastrowid
        conn.close()

        # 2. Формируем имена для локального сохранения и облака
        safe_name = name.replace(" ", "_")
        safe_settings = f"{format}_{paper}_{crop}".replace(" ", "_")
        remote_folder_path = f"PhotoDoc_Orders/Заказ_{order_id}_{safe_name}/{safe_settings}"

        for file, safe_filename in zip(files, safe_filenames):
            local_filename = f"{order_id}_{safe_filename}"
            local_file_path = (UPLOAD_DIR / local_filename).resolve()
            if UPLOAD_DIR.resolve() not in local_file_path.parents:
                raise HTTPException(status_code=400, detail="Invalid filename")
            
            # 3. Сохраняем физический файл на жесткий диск
            with open(local_file_path, "wb") as f:
                content = await file.read()
                f.write(content)

            # 4. Формируем имя файла для Яндекс.Диска
            remote_file_name = f"{order_id}_{safe_filename}"

            # 5. Передаем работу в фоновые задачи (для каждого файла)
            background_tasks.add_task(upload_to_yandex_disk, local_file_path, remote_folder_path, remote_file_name)
            
        # Техническое письмо администратору отправляем сразу при создании заказа.
        background_tasks.add_task(
            send_admin_order_email,
            order_id,
            name,
            phone,
            email.strip(),
            comment,
            format,
            paper,
            crop,
            filenames,
            remote_folder_path,
        )

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
