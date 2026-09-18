import os
import logging
import sqlite3
import hashlib
import smtplib
import urllib.parse
from pathlib import Path
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Optional

import httpx
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка базового логирования
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="PhotoDoc AI API")

# Настройка CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "orders.db"))
UPLOAD_DIR = BASE_DIR / "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


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
            order_amount TEXT,
            payment_amount TEXT,
            payment_method TEXT DEFAULT 'robokassa',
            robokassa_inv_id TEXT,
            paid_at TEXT,
            payment_email_sent_at TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


init_db()


@app.get("/api/health")
def health_check():
    return {"status": "ok"}


# --- Known server SKUs (explicit allowlist — no unknown combinations accepted) ---
# Format: SERVICE_CODE -> (price_per_unit, is_per_file)
_DOC_SKUS: dict[str, float] = {
    "DOC_3X4": 300.0,
    "DOC_35X45": 300.0,
    "DOC_4X6": 300.0,
    "DOC_9X12": 300.0,
}
_PRINT_SKUS: dict[str, float] = {
    "PRINT_9X13": 20.0,
    "PRINT_10X15": 20.0,
    "PRINT_13X18": 40.0,
    "PRINT_15X20": 40.0,
    "PRINT_A4": 70.0,
    "PRINT_30X40": 150.0,
}
_RESTORE_SKUS: dict[str, float] = {
    "RESTORE_LIGHT": 200.0,
    "RESTORE_DEEP": 350.0,
    "RESTORE_COLORIZE": 150.0,
}
_POLAROID_SKU = "POLAROID"

# Legacy display-name → normalized SKU mapping (for orders created via UI)
_LEGACY_FORMAT_TO_SKU: dict[str, str] = {
    "3x4": "DOC_3X4",
    "3.5x4.5": "DOC_35X45",
    "4x6": "DOC_4X6",
    "9x12": "DOC_9X12",
    "9x13": "PRINT_9X13",
    "10x15": "PRINT_10X15",
    "13x18": "PRINT_13X18",
    "15x20": "PRINT_15X20",
    "A4": "PRINT_A4",
    "30x40": "PRINT_30X40",
    "polaroid": "POLAROID",
    "Polaroid": "POLAROID",
    "POLAROID": "POLAROID",
    "Легкая реставрация": "RESTORE_LIGHT",
    "Глубокая реставрация с ИИ": "RESTORE_DEEP",
    "Окрашивание / Колоризация": "RESTORE_COLORIZE",
}


def _resolve_sku(format_str: str, crop_str: str) -> Optional[str]:
    """Resolves UI input into a canonical server SKU, or returns None if unrecognised."""
    format_val = (format_str or "").strip()
    crop_val = (crop_str or "").strip()

    # Direct SKU match
    all_skus = set(_DOC_SKUS) | set(_PRINT_SKUS) | set(_RESTORE_SKUS) | {_POLAROID_SKU}
    if format_val in all_skus:
        return format_val
    if crop_val in all_skus:
        return crop_val

    # Legacy display-name lookup
    if format_val in _LEGACY_FORMAT_TO_SKU:
        return _LEGACY_FORMAT_TO_SKU[format_val]
    if crop_val in _LEGACY_FORMAT_TO_SKU:
        return _LEGACY_FORMAT_TO_SKU[crop_val]

    return None


# --- Серверная система расчета стоимости (Authoritative Price Engine) ---
def calculate_authoritative_price(format_str: str, paper_str: str, crop_str: str, file_count: int) -> float:
    """
    Вычисляет строго серверную стоимость заказа на основе типа услуги и параметров.
    Принимает ТОЛЬКО явно известные серверные SKU или их legacy-псевдонимы.
    Неизвестные комбинации вызывают HTTPException 400 — НЕ дефолт 300 руб.
    """
    count = max(1, file_count)
    sku = _resolve_sku(format_str, crop_str)

    if sku is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown service/SKU: format='{format_str}', crop='{crop_str}'. "
                   "Accepted SKUs: DOC_3X4, DOC_35X45, DOC_4X6, DOC_9X12, POLAROID, "
                   "PRINT_9X13, PRINT_10X15, PRINT_13X18, PRINT_15X20, PRINT_A4, PRINT_30X40, "
                   "RESTORE_LIGHT, RESTORE_DEEP, RESTORE_COLORIZE.",
        )

    if sku in _DOC_SKUS:
        return _DOC_SKUS[sku]  # flat per-order price, not per-file
    if sku == _POLAROID_SKU:
        return 30.0 * count
    if sku in _RESTORE_SKUS:
        return _RESTORE_SKUS[sku] * count
    if sku in _PRINT_SKUS:
        return _PRINT_SKUS[sku] * count

    # Should be unreachable after _resolve_sku, but be explicit
    raise HTTPException(status_code=400, detail=f"Unhandled SKU: {sku}")


def format_robokassa_amount(amount: float | str | int) -> str:
    """Приводит сумму к стандартному формату с двумя знаками после запятой (например: '300.00')."""
    try:
        val = float(str(amount).replace(",", ".").strip())
        if val <= 0:
            raise ValueError("Amount must be positive")
        return f"{val:.2f}"
    except (ValueError, TypeError) as err:
        raise ValueError(f"Invalid amount value: {amount}") from err


def get_order_by_id(order_id: int) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def get_authoritative_order_amount(order: dict) -> str:
    """
    Возвращает авторитетную стоимость заказа на основе данных в БД.
    """
    if order.get("order_amount"):
        try:
            return format_robokassa_amount(order["order_amount"])
        except ValueError:
            pass

    # payment_amount is transactional payment state ONLY — never a price authority.
    # If order_amount is missing/invalid, recalculate server-side from order spec.
    filenames = (order.get("filename") or "").split(",")
    file_count = len([f for f in filenames if f.strip()])
    price = calculate_authoritative_price(
        order.get("format", ""),
        order.get("paper", ""),
        order.get("crop", ""),
        file_count,
    )
    return format_robokassa_amount(price)


def update_order_payment_pending(order_id: int, amount: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE orders
        SET payment_amount = ?, payment_status = 'pending', status = 'awaiting_payment'
        WHERE id = ?
        """,
        (amount, order_id),
    )
    conn.commit()
    conn.close()


def mark_order_paid(order_id: int, amount: str) -> bool:
    """
    Atomically transitions order to 'paid' status using a conditional UPDATE.
    Returns True if THIS call performed the transition (rowcount == 1).
    Returns False if order was already paid by a concurrent request (idempotent, no side-effects).
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    paid_timestamp = datetime.utcnow().isoformat()
    cursor.execute(
        """
        UPDATE orders
        SET payment_status = 'paid', status = 'paid', payment_amount = ?, paid_at = ?
        WHERE id = ? AND payment_status != 'paid'
        """,
        (amount, paid_timestamp, order_id),
    )
    conn.commit()
    was_transition_owner = cursor.rowcount == 1
    conn.close()
    return was_transition_owner


def sanitize_upload_filename(filename: str | None) -> str:
    base_name = (filename or "upload").replace("\\", "/").split("/")[-1]
    cleaned = "".join(c for c in base_name if c.isalnum() or c in "._- ")
    return cleaned.strip() or "upload"


# --- Фоновые задачи ---
def send_order_email(order_id: int, name: str, phone: str, comment: str, format: str = "Не указан", paper: str = "Не указана", crop: str = "Не указано"):
    smtp_server = os.getenv("SMTP_SERVER")
    smtp_port = int(os.getenv("SMTP_PORT", 465))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    email_to = os.getenv("EMAIL_TO")

    if not all([smtp_server, smtp_user, smtp_password, email_to]):
        logging.warning("SMTP настройки не заданы в .env — отправка email пропущена.")
        return

    msg = MIMEMultipart()
    msg["From"] = smtp_user
    msg["To"] = email_to
    msg["Subject"] = f"Новый заказ #{order_id} | PhotoDoc AI"

    safe_folder_name = f"Заказ_{order_id}_{name.replace(' ', '_')}"
    raw_path = f"PhotoDoc_Orders/{safe_folder_name}"
    safe_url_path = urllib.parse.quote(raw_path)
    yandex_disk_link = f"https://disk.yandex.ru/client/disk/{safe_url_path}"

    body = (
        f"Новый заказ!\nНомер: {order_id}\nИмя: {name}\nТелефон: {phone}\n"
        f"Формат: {format}\nБумага: {paper}\nКадрирование: {crop}\nКомментарий: {comment}\n\n"
        f"Ссылка на Яндекс.Диск: {yandex_disk_link}"
    )
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        logging.info("Email для заказа %s успешно отправлен.", order_id)
    except Exception as e:
        logging.error("Ошибка при отправке Email заказа %s: %s", order_id, e)


def send_paid_order_email(order_id: int):
    """
    Sends payment confirmation email exactly once per order.
    payment_email_sent_at is recorded ONLY after a successful SMTP send,
    so failed sends leave the timestamp NULL and remain retryable.
    """
    order = get_order_by_id(order_id)
    if not order:
        return

    # Idempotency guard: skip if email was already successfully sent
    if order.get("payment_email_sent_at"):
        logging.info("Email об оплате заказа #%s уже был отправлен ранее, пропуск.", order_id)
        return

    smtp_server = os.getenv("SMTP_SERVER")
    smtp_port = int(os.getenv("SMTP_PORT", 465))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    email_to = os.getenv("EMAIL_TO")

    if not all([smtp_server, smtp_user, smtp_password, email_to]):
        # SMTP not configured — do NOT record payment_email_sent_at so retry stays possible
        logging.warning(
            "SMTP не настроен — уведомление об оплате заказа #%s НЕ отправлено. "
            "payment_email_sent_at остаётся NULL для возможности повтора.",
            order_id,
        )
        return

    msg = MIMEMultipart()
    msg["From"] = smtp_user
    msg["To"] = email_to
    msg["Subject"] = f"✅ Заказ #{order_id} ОПЛАЧЕН ({order.get('payment_amount', '0.00')} руб.) | PhotoDoc AI"
    body = (
        f"Заказ #{order_id} успешно оплачен!\n"
        f"Клиент: {order.get('name')}\n"
        f"Телефон: {order.get('phone')}\n"
        f"Сумма оплаты: {order.get('payment_amount')} руб.\n"
        f"Дата оплаты: {order.get('paid_at')}\n"
    )
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        # Record timestamp ONLY after successful send
        now_iso = datetime.utcnow().isoformat()
        conn = sqlite3.connect(DB_PATH)
        conn.cursor().execute(
            "UPDATE orders SET payment_email_sent_at = ? WHERE id = ?", (now_iso, order_id)
        )
        conn.commit()
        conn.close()
        logging.info("Email об оплате заказа #%s успешно отправлен.", order_id)
    except Exception as e:
        # SMTP failure: payment_email_sent_at stays NULL → retry remains possible
        logging.error("Ошибка при отправке email об оплате заказа #%s: %s", order_id, e)


def ensure_yandex_folder(token: str, path: str):
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
                timeout=10,
            )
        except Exception:
            pass


def upload_to_yandex_disk(local_file_path: str, remote_folder_path: str, remote_file_name: str):
    token = os.getenv("YANDEX_DISK_TOKEN")
    if not token:
        return

    ensure_yandex_folder(token, remote_folder_path)
    headers = {"Authorization": f"OAuth {token}"}
    remote_path = f"{remote_folder_path}/{remote_file_name}"

    try:
        upload_url_response = httpx.get(
            "https://cloud-api.yandex.net/v1/disk/resources/upload",
            headers=headers,
            params={"path": remote_path, "overwrite": "true"},
            timeout=30,
        )
        upload_url_response.raise_for_status()
        href = upload_url_response.json().get("href")
        if not href:
            return

        with open(local_file_path, "rb") as f:
            put_response = httpx.put(href, content=f.read(), timeout=60)
            put_response.raise_for_status()
    except Exception as e:
        logging.error("Ошибка загрузки на Яндекс.Диск: %s", e)


def upload_info_to_yandex_disk(order_id: int, name: str, phone: str, format: str, paper: str, crop: str, comment: str, remote_folder_path: str):
    token = os.getenv("YANDEX_DISK_TOKEN")
    if not token:
        return

    ensure_yandex_folder(token, remote_folder_path)
    order_info_text = (
        f"Заказ №: {order_id}\nИмя: {name}\nТелефон: {phone}\n"
        f"Формат: {format}\nБумага: {paper}\nКадрирование: {crop}\nКомментарий: {comment}"
    )
    headers = {"Authorization": f"OAuth {token}"}
    info_file_path = f"{remote_folder_path}/info.txt"

    try:
        res = httpx.get(
            "https://cloud-api.yandex.net/v1/disk/resources/upload",
            headers=headers,
            params={"path": info_file_path, "overwrite": "true"},
            timeout=30,
        )
        if res.status_code == 200:
            upload_url = res.json().get("href")
            if upload_url:
                httpx.put(upload_url, content=order_info_text.encode("utf-8"), timeout=30)
    except Exception as e:
        logging.error("Ошибка загрузки info.txt: %s", e)


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
    files: List[UploadFile] = File(...),
):
    try:
        safe_filenames = [sanitize_upload_filename(f.filename) for f in files]
        filenames_joined = ", ".join(safe_filenames)

        # Вычисляем авторитетную стоимость на сервере
        order_price = calculate_authoritative_price(format, paper, crop, len(files))
        formatted_order_amount = format_robokassa_amount(order_price)

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO orders (
                name, phone, email, format, paper, crop, comment, filename,
                status, payment_status, order_amount, payment_amount
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', 'unpaid', ?, ?)
            """,
            (
                name.strip(),
                phone.strip(),
                email.strip(),
                format.strip(),
                paper.strip(),
                crop.strip(),
                comment.strip(),
                filenames_joined,
                formatted_order_amount,
                formatted_order_amount,
            ),
        )
        conn.commit()
        order_id = cursor.lastrowid
        conn.close()

        safe_name = name.replace(" ", "_")
        safe_settings = f"{format}_{paper}_{crop}".replace(" ", "_")
        remote_folder_path = f"PhotoDoc_Orders/Заказ_{order_id}_{safe_name}/{safe_settings}"

        for file, safe_filename in zip(files, safe_filenames):
            local_filename = f"{order_id}_{safe_filename}"
            local_file_path = (UPLOAD_DIR / local_filename).resolve()
            if UPLOAD_DIR.resolve() not in local_file_path.parents:
                raise HTTPException(status_code=400, detail="Invalid filename")

            content = await file.read()
            with open(local_file_path, "wb") as f:
                f.write(content)

            background_tasks.add_task(upload_to_yandex_disk, str(local_file_path), remote_folder_path, local_filename)

        background_tasks.add_task(send_order_email, order_id, name, phone, comment, format, paper, crop)
        background_tasks.add_task(upload_info_to_yandex_disk, order_id, name, phone, format, paper, crop, comment, remote_folder_path)

        return {
            "ok": True,
            "message": "Заказ успешно создан",
            "order_id": order_id,
            "amount": formatted_order_amount,
        }

    except HTTPException:
        raise
    except Exception as e:
        logging.error("Критическая ошибка при создании заказа: %s", e)
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")


# --- Эндпоинты платежной системы Robokassa ---
class PaymentCreateRequest(BaseModel):
    order_id: int | str
    amount: Optional[float | str] = None
    email: Optional[str] = None


@app.post("/api/payment/create")
async def create_payment(payment: PaymentCreateRequest):
    merchant_login = os.getenv("ROBOKASSA_MERCHANT_LOGIN")
    password1 = os.getenv("ROBOKASSA_PASSWORD1")
    is_test_val = os.getenv("ROBOKASSA_IS_TEST", "1")

    if not merchant_login or not password1:
        raise HTTPException(status_code=500, detail="Robokassa settings are not configured")

    try:
        order_id_int = int(str(payment.order_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="order_id must be an integer")

    order = get_order_by_id(order_id_int)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # 1. Авторитетное определение стоимости на стороне сервера
    authoritative_amount = get_authoritative_order_amount(order)

    # 2. Если клиент передал сумму, проверяем на несоответствие
    if payment.amount is not None:
        try:
            client_amount_formatted = format_robokassa_amount(payment.amount)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid client amount")

        if client_amount_formatted != authoritative_amount:
            raise HTTPException(
                status_code=400,
                detail=f"Payment amount mismatch: order price is {authoritative_amount}, but client requested {client_amount_formatted}",
            )

    # 3. Сумма к оплате всегда берется строго из авторитетного источника сервера
    amount_to_pay = authoritative_amount
    update_order_payment_pending(order_id_int, amount_to_pay)

    order_id_str = str(order_id_int)
    customer_email = (payment.email or order.get("email") or "").strip()
    signature_source = f"{merchant_login}:{amount_to_pay}:{order_id_str}:{password1}"
    signature = hashlib.md5(signature_source.encode("utf-8")).hexdigest()

    params = {
        "MerchantLogin": merchant_login,
        "OutSum": amount_to_pay,
        "InvId": order_id_str,
        "Description": f"Заказ_{order_id_str}",
        "SignatureValue": signature,
        "IsTest": is_test_val,
    }
    if customer_email:
        params["Email"] = customer_email

    payment_url = "https://auth.robokassa.ru/Merchant/Index.aspx?" + urllib.parse.urlencode(params)
    return {
        "ok": True,
        "order_id": order_id_int,
        "amount": amount_to_pay,
        "payment_url": payment_url,
    }


async def read_robokassa_payload(request: Request) -> dict:
    payload = dict(request.query_params)
    if request.method.upper() == "POST":
        try:
            form_data = await request.form()
            payload.update(dict(form_data))
        except Exception:
            pass
    return payload


@app.api_route("/api/payment/robokassa/result", methods=["GET", "POST"])
async def robokassa_result(request: Request, background_tasks: BackgroundTasks):
    """
    АВТОРИТЕТНЫЙ server-to-server callback от Robokassa.
    Проверяется подпись Password2, сверяется сумма, статус атомарно переводится в 'paid'.
    Идемпотентность: повторные валидные вызовы возвращают OK{order_id} без дублирования email.
    """
    password2 = os.getenv("ROBOKASSA_PASSWORD2")
    if not password2:
        logging.error("Result URL: ROBOKASSA_PASSWORD2 не задан в .env")
        raise HTTPException(status_code=500, detail="Robokassa password2 is not configured")

    payload = await read_robokassa_payload(request)
    out_sum = payload.get("OutSum")
    inv_id = payload.get("InvId")
    signature = payload.get("SignatureValue")

    if not out_sum or inv_id is None or not signature:
        logging.error("Result URL: Отсутствуют обязательные параметры payload=%s", payload)
        raise HTTPException(status_code=400, detail="Missing Robokassa payload")

    try:
        order_id = int(str(inv_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid order id")

    # 1. Проверка подписи Password2 (сервер-сервер)
    expected_sig = hashlib.md5(f"{out_sum}:{inv_id}:{password2}".encode("utf-8")).hexdigest()
    if expected_sig.lower() != str(signature).lower():
        logging.warning("Result URL: Неверная подпись для InvId=%s", inv_id)
        raise HTTPException(status_code=400, detail="Invalid signature")

    # 2. Проверка существования заказа
    order = get_order_by_id(order_id)
    if not order:
        logging.error("Result URL: Заказ #%s не найден", order_id)
        raise HTTPException(status_code=404, detail="Order not found")

    # 3. Сверка суммы платежа с авторитетной суммой заказа
    expected_amount = get_authoritative_order_amount(order)
    if format_robokassa_amount(out_sum) != expected_amount:
        logging.error(
            "Result URL: Несовпадение суммы для заказа #%s: получено=%s, ожидалось=%s",
            order_id,
            out_sum,
            expected_amount,
        )
        raise HTTPException(status_code=400, detail="Amount mismatch")

    # 4. Atomic transition to 'paid' (conditional UPDATE WHERE payment_status != 'paid')
    #    Returns True if THIS request performed the transition; False if already paid (concurrent duplicate).
    was_transition_owner = mark_order_paid(order_id, expected_amount)

    if was_transition_owner:
        logging.info("Result URL: Заказ #%s успешно отмечен как оплачен.", order_id)
        # Schedule email only for the one call that owned the transition
        background_tasks.add_task(send_paid_order_email, order_id)
    else:
        logging.info(
            "Result URL: Заказ #%s уже отмечен как оплачен (конкурентный дубликат). "
            "Идемпотентный ответ OK%s.",
            order_id,
            order_id,
        )

    return PlainTextResponse(f"OK{order_id}")


@app.api_route("/api/payment/robokassa/success", methods=["GET", "POST"])
async def robokassa_success(request: Request):
    """
    НЕАВТОРИТЕТНЫЙ редирект возврата клиента в браузер.
    Никогда не переводит заказ в статус 'paid' самостоятельно.
    Только отображает текущее состояние заказа, подтвержденное Result URL.
    """
    password1 = os.getenv("ROBOKASSA_PASSWORD1")
    if not password1:
        raise HTTPException(status_code=500, detail="Robokassa password1 is not configured")

    payload = await read_robokassa_payload(request)
    out_sum = payload.get("OutSum")
    inv_id = payload.get("InvId")
    signature = payload.get("SignatureValue")

    if not out_sum or inv_id is None or not signature:
        return JSONResponse(
            {"ok": False, "status": "unknown", "message": "Параметры платежа не переданы"},
            status_code=400,
        )

    try:
        order_id = int(str(inv_id))
    except ValueError:
        return JSONResponse(
            {"ok": False, "status": "unknown", "message": "Некорректный номер заказа"},
            status_code=400,
        )

    expected_sig = hashlib.md5(f"{out_sum}:{inv_id}:{password1}".encode("utf-8")).hexdigest()
    if expected_sig.lower() != str(signature).lower():
        raise HTTPException(status_code=400, detail="Invalid signature")

    order = get_order_by_id(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # Success URL НЕ изменяет статус заказа на 'paid'!
    is_paid = order.get("payment_status") == "paid"

    return JSONResponse({
        "ok": True,
        "order_id": order_id,
        "status": "paid" if is_paid else "pending",
        "message": "Оплата подтверждена" if is_paid else "Ожидание подтверждения от платежной системы",
    })
