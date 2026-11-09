import os
import logging
import math
import sqlite3
import hashlib
import hmac
import smtplib
import urllib.parse
import secrets
import uuid
import threading
import tempfile
import re
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Optional

import httpx

# Sanitize NO_PROXY on Windows against httpx unbracketed IPv6 ::1 parser issue
for _k in ("NO_PROXY", "no_proxy"):
    if _k in os.environ and "::1" in os.environ[_k] and "[::1]" not in os.environ[_k]:
        os.environ[_k] = os.environ[_k].replace("::1", "")

import operator_workflow as operator_service
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

from db_contract import APPLICATION_ID, inspect_database, read_application_id
from sqlite_busy import BUSY_TIMEOUT, retry_busy
import paid_email_outbox
from yandex_paths import (
    is_production_disk_path,
    is_within as _within_yandex_root,
    join_path as _join_yandex_path,
    normalize_path as _normalize_yandex_path,
    split_path as _split_yandex_path,
)
from delivery import (
    DELIVERY_SCHEMA,
    cleanup_orphaned_temporary_files,
    enqueue,
    perform_job_cleanup,
    process_due,
    process_due_cleanups,
    set_uploads_dir,
    worker_loop,
)

# Загрузка переменных окружения
load_dotenv()

# Настройка базового логирования
logging.basicConfig(level=logging.INFO)

KNOWN_STAGING_PLACEHOLDERS = {
    "y0_test_staging_token_mock",
    "y0_example_staging_token...",
    "mock",
    "placeholder",
    "test",
    "test-oauth-token",
    "changeme",
}


def get_yandex_disk_root() -> str:
    root = (os.getenv("YANDEX_DISK_ROOT_DIR") or "PhotoDoc_Orders").strip()
    if not root.strip("/"):
        return "PhotoDoc_Orders"  # Preserve the legacy empty/slash-only fallback.
    return _normalize_yandex_path(root or "PhotoDoc_Orders")


def is_staging_environment() -> bool:
    return (
        os.getenv("PHOTODOC_STAGING", "0").strip().lower() in ("1", "true")
        or os.getenv("PHOTODOC_ALLOW_FRESH_DB", "0").strip().lower() in ("1", "true")
        or (os.getenv("YANDEX_DISK_ROOT_DIR") or "").strip().lower().startswith("photodoc_staging")
    )


def validate_staging_delivery_safety():
    """
    Enforces fail-closed safety for delivery in staging environments:
    1. Staging delivery must never target the production root 'PhotoDoc_Orders'.
    2. Staging delivery requires an explicit, genuine token and rejects known placeholders.
    """
    if not is_staging_environment():
        return

    root = get_yandex_disk_root()
    if is_production_disk_path(root):
        raise RuntimeError(
            f"Staging delivery configuration forbidden to target production root '{root}'"
        )

    token = (os.getenv("YANDEX_DISK_TOKEN") or os.getenv("YANDEX_DISK_STAGING_TOKEN") or "").strip()
    if not token:
        raise RuntimeError(
            "Staging delivery worker requires explicit non-empty YANDEX_DISK_STAGING_TOKEN"
        )
    token_lower = token.lower()
    if (
        token_lower in KNOWN_STAGING_PLACEHOLDERS
        or "placeholder" in token_lower
        or "example" in token_lower
        or token_lower.endswith("_mock")
    ):
        if os.getenv("PHOTODOC_TEST_MOCK_TOKEN_ALLOWED", "0") != "1":
            raise RuntimeError(
                "Staging delivery worker cannot use placeholder or example token"
            )


@asynccontextmanager
async def lifespan(_app):
    stop = threading.Event()
    thread = None
    if os.getenv("DELIVERY_WORKER_ENABLED", "1") != "0":
        validate_staging_delivery_safety()
        thread = threading.Thread(target=worker_loop, args=(DB_PATH, deliver_job, stop), kwargs={"uploads_dir": UPLOAD_DIR}, daemon=True)
        thread.start()
    try:
        yield
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=5)


app = FastAPI(title="PhotoDoc AI API", lifespan=lifespan)

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
set_uploads_dir(UPLOAD_DIR)


# --- Инициализация базы данных ---
def init_db():
    """Create only a genuinely fresh DB; never bless an existing unknown DB."""
    db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0

    if db_size > 0:
        ready, _ = inspect_database(DB_PATH, require_complete_schema=True)
        if not ready:
            raise RuntimeError(
                "Existing database does not satisfy the PhotoDoc identity/schema contract; "
                "run the controlled reconciliation migration"
            )
        paid_email_outbox.initialize(DB_PATH)
        return

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(f"PRAGMA application_id = {APPLICATION_ID}")
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
            order_token_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        for statement in DELIVERY_SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        if read_application_id(conn) != APPLICATION_ID:
            raise RuntimeError("Failed to initialize PhotoDoc database identity")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    ready, _ = inspect_database(DB_PATH, require_complete_schema=True)
    if not ready:
        raise RuntimeError("Fresh PhotoDoc database failed post-initialization verification")


def database_readiness_ok() -> bool:
    ready, _ = inspect_database(DB_PATH, require_complete_schema=True)
    return ready


def _require_existing_database_identity() -> bool:
    return os.getenv("PHOTODOC_REQUIRE_DB_IDENTITY", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _allow_fresh_db() -> bool:
    return os.getenv("PHOTODOC_ALLOW_FRESH_DB", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def ensure_database_state():
    """Manage database startup state with strict production fail-closed semantics."""
    db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0

    if _require_existing_database_identity():
        if db_size == 0:
            if _allow_fresh_db():
                init_db()
            else:
                raise RuntimeError(
                    "Production database missing or empty; automatic DB creation is "
                    "forbidden under PHOTODOC_REQUIRE_DB_IDENTITY=1"
                )
        else:
            ready, diag = inspect_database(DB_PATH, require_complete_schema=True)
            if not ready:
                raise RuntimeError(
                    "Existing production database does not satisfy the PhotoDoc identity/schema contract"
                )
    else:
        init_db()

    if not database_readiness_ok():
        raise RuntimeError("Production database identity/readiness verification failed")
    paid_email_outbox.initialize(DB_PATH)


ensure_database_state()


REQUIRED_CLEANUP_COLUMNS = {
    "cleanup_state",
    "cleanup_phase",
    "local_deleted_at",
    "cleanup_error",
    "cleanup_attempts",
    "cleanup_lease_until",
}


def delivery_schema_ready() -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_jobs'").fetchone()
        delivery_cols = {row[1] for row in conn.execute("PRAGMA table_info(delivery_jobs)")} if table else set()
        indexes = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='delivery_jobs'"
        )} if table else set()
        return (
            "order_token_hash" in columns
            and table is not None
            and REQUIRED_CLEANUP_COLUMNS <= delivery_cols
            and {"idx_delivery_due", "idx_cleanup_due", "idx_paid_email_once", "idx_admin_email_once"} <= indexes
        )
    finally:
        conn.close()


if not delivery_schema_ready():
    raise RuntimeError("P1 delivery schema is missing; run reconcile_delivery_schema.py")


@app.get("/api/health")
def health_check():
    if not database_readiness_ok() or not delivery_schema_ready():
        return JSONResponse({"status": "unavailable"}, status_code=503)
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
        if not math.isfinite(val) or val <= 0:
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


class PaymentStateError(RuntimeError):
    """The persisted payment state cannot safely perform the requested transition."""


def update_order_payment_pending(order_id: int, amount: str) -> str:
    """Atomically perform unpaid -> pending, or return an idempotent terminal state."""
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE orders
            SET payment_amount = ?, payment_status = 'pending', status = 'awaiting_payment'
            WHERE id = ? AND payment_status = 'unpaid'
            """,
            (amount, order_id),
        )
        if cursor.rowcount == 1:
            conn.commit()
            return "pending"

        row = cursor.execute(
            "SELECT payment_status, payment_amount FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        conn.commit()
        if row is None:
            return "not_found"
        state = row["payment_status"]
        if state == "paid":
            return "paid"
        if state == "pending":
            try:
                persisted_amount = format_robokassa_amount(row["payment_amount"])
            except ValueError as exc:
                raise PaymentStateError("Pending payment has an invalid amount") from exc
            if persisted_amount != amount:
                raise PaymentStateError("Pending payment amount does not match order amount")
            return "pending"
        raise PaymentStateError("Unknown payment state")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@retry_busy
def _flag_missing_sources(conn, order_id: int) -> None:
    """A paid order must never look processable when its source files are gone.

    Retention deletes the source of an order that stayed unpaid past its window; if the
    provider confirms a payment for such an order afterwards, the money is real and the
    order must stay visible instead of silently waiting for a file that no longer exists.
    """
    rows = conn.execute(
        "SELECT id, local_path, state FROM delivery_jobs "
        "WHERE order_id = ? AND kind IN ('file', 'info')",
        (order_id,),
    ).fetchall()
    missing = [row[0] for row in rows
               if row[2] != "delivered" and row[1] and not Path(row[1]).exists()]
    if not missing:
        return
    for job_id in missing:
        conn.execute(
            "UPDATE delivery_jobs SET last_error = 'SourceMissingAtPayment' WHERE id = ?",
            (job_id,),
        )
    logging.critical(
        "Late payment for order #%s: %s source file(s) are missing; the order needs a "
        "manual resolution (deliver from another source or refund).",
        order_id,
        len(missing),
    )


def mark_order_paid(order_id: int, amount: str) -> bool:
    """
    Atomically transitions order to 'paid' status using a conditional UPDATE.
    Returns True if THIS call performed the transition (rowcount == 1).
    Returns False if order was already paid by a concurrent request (idempotent, no side-effects).
    """
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT)
    try:
        cursor = conn.cursor()
        paid_timestamp = datetime.utcnow().isoformat()
        cursor.execute(
            """
            UPDATE orders
            SET payment_status = 'paid', status = 'paid', payment_amount = ?, paid_at = ?
            WHERE id = ? AND payment_status IN ('unpaid', 'pending')
            """,
            (amount, paid_timestamp, order_id),
        )
        if cursor.rowcount == 1:
            enqueue(conn, order_id=order_id, kind="paid_email")
            _flag_missing_sources(conn, order_id)
            conn.commit()
            return True
        conn.commit()
        row = cursor.execute(
            "SELECT payment_status FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if row and row[0] == "paid":
            return False
        raise PaymentStateError("Order cannot transition to paid from its current state")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sanitize_upload_filename(filename: str | None) -> str:
    base_name = (filename or "upload").replace("\\", "/").split("/")[-1]
    cleaned = "".join(c for c in base_name if c.isalnum() or c in "._- ")
    return cleaned.strip() or "upload"


MAX_FILES = 10
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ORDER_BYTES = 80 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 64 * 1024
IMAGE_TYPES = {"image/jpeg": ("jpg", b"\xff\xd8\xff"),
               "image/png": ("png", b"\x89PNG\r\n\x1a\n"),
               "image/webp": ("webp", b"RIFF")}


def _folder_component(value: str) -> str:
    component = re.sub(r"[^\w-]+", "_", value.strip(), flags=re.UNICODE).strip("_")[:100]
    return component or "Не_указано"


async def _stage_upload(file: UploadFile, used_bytes: int) -> dict:
    if file.content_type not in IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Only JPEG, PNG and WebP images are accepted")
    descriptor, temporary_path = tempfile.mkstemp(prefix=".incoming-", dir=UPLOAD_DIR)
    path = Path(temporary_path)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor, "wb") as output:
            first = True
            while chunk := await file.read(UPLOAD_CHUNK_BYTES):
                if first:
                    suffix, magic = IMAGE_TYPES[file.content_type]
                    valid = chunk.startswith(magic)
                    if file.content_type == "image/webp":
                        valid = valid and chunk[8:12] == b"WEBP"
                    if not valid:
                        raise HTTPException(status_code=400, detail="Image content does not match its type")
                    first = False
                size += len(chunk)
                if size > MAX_FILE_BYTES or used_bytes + size > MAX_ORDER_BYTES:
                    raise HTTPException(status_code=413, detail="Upload size limit exceeded")
                output.write(chunk)
                digest.update(chunk)
            if size == 0:
                raise HTTPException(status_code=400, detail="Empty image")
            output.flush()
            os.fsync(output.fileno())
        return {"path": path, "size": size, "sha256": digest.hexdigest(),
                "original_name": file.filename or "upload", "suffix": suffix}
    except Exception:
        path.unlink(missing_ok=True)
        raise


# --- Фоновые задачи ---
#: Fallback domain for outbound Message-IDs when nothing else is configured.
DEFAULT_MAIL_DOMAIN = "photo1storage.ru"

_DOMAIN_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_MAIL_DOMAIN_RE = re.compile(rf"^{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})+$")


def get_mail_domain() -> str:
    """Domain used in outbound ``Message-ID`` headers.

    Message identity must match the domain the site is actually served from, or
    recipients applying DMARC alignment will treat the mail as unaligned. The
    value is configurable (``PHOTODOC_MAIL_DOMAIN``) and otherwise derived from
    the authenticated mailbox, falling back to the canonical domain.

    The result is strictly validated as a dotted DNS name. That is a security
    requirement, not a convenience: the value is interpolated straight into a
    message header, so anything permitting CR/LF or angle brackets would be a
    header-injection primitive. An invalid value falls back rather than raising,
    because a malformed envelope must not take down order notifications.
    """
    candidate = (os.getenv("PHOTODOC_MAIL_DOMAIN") or "").strip().lower()
    if not candidate:
        mailbox = (os.getenv("SMTP_USER") or "").strip()
        if "@" in mailbox:
            candidate = mailbox.rsplit("@", 1)[1].strip().lower()
    if candidate and _MAIL_DOMAIN_RE.fullmatch(candidate):
        return candidate
    return DEFAULT_MAIL_DOMAIN


def message_id(order_id: int, kind: str) -> str:
    """Stable Message-ID for an order notification, on the configured domain."""
    assert kind in ("new", "paid")
    return f"<photodoc-order-{order_id}-{kind}@{get_mail_domain()}>"


def send_order_email(order_id: int, name: str, phone: str, comment: str, format: str = "Не указан", paper: str = "Не указана", crop: str = "Не указано"):
    smtp_server = os.getenv("SMTP_SERVER")
    smtp_port = int(os.getenv("SMTP_PORT", 465))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    email_to = os.getenv("EMAIL_TO")

    if not all([smtp_server, smtp_user, smtp_password, email_to]):
        logging.warning("SMTP настройки не заданы в .env — отправка email пропущена.")
        return False

    msg = MIMEMultipart()
    msg["From"] = smtp_user
    msg["To"] = email_to
    msg["Subject"] = f"Новый заказ #{order_id} | PhotoDoc AI"
    msg["Message-ID"] = message_id(order_id, "new")

    safe_folder_name = f"Заказ_{order_id}_{name.replace(' ', '_')}"
    raw_path = f"{get_yandex_disk_root()}/{safe_folder_name}"
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
        return True
    except Exception as e:
        logging.error("Ошибка при отправке Email заказа %s: %s", order_id, e)
        return False


def _smtp_dot_stuff(payload: bytes) -> bytes:
    """SMTP transparency (RFC 5321 §4.5.2): double a leading '.' on any line."""
    if payload.startswith(b"."):
        payload = b"." + payload
    return payload.replace(b"\r\n.", b"\r\n..")


def _smtp_transport(host: str, port: int, timeout: float):
    """Open an SMTP_SSL client that reports where the DATA transaction reached.

    ``smtplib.send_message()`` hides the moment the server accepts the DATA
    command (354). That moment is the only protocol-level evidence separating
    "nothing was transmitted" from "content may have been accepted", so it is
    observed directly instead of being inferred from exception types.

    A substitute transport (test double, mock) cannot report protocol state and
    is used unchanged; its failures are consequently treated as unproven.
    """
    base = smtplib.SMTP_SSL
    if not (isinstance(base, type) and issubclass(base, smtplib.SMTP)):
        return base(host, port, timeout=timeout)

    class _ObservedSMTPSSL(base):
        content_committed = False

        def data(self, payload):
            self.putcmd("data")
            code, reply = self.getreply()
            if code != 354:
                # Refused before any content, so the server cannot hold it.
                raise smtplib.SMTPDataError(code, reply)
            # 354 accepted: message content starts flowing from here.
            self.content_committed = True
            if isinstance(payload, str):
                # Mirror smtplib.SMTP.data: normalise line endings for text input.
                payload = re.sub(r"(?:\r\n|\n|\r(?!\n))", "\r\n", payload).encode("ascii")
            body = _smtp_dot_stuff(payload)
            if not body.endswith(b"\r\n"):
                body += b"\r\n"
            self.send(body + b".\r\n")
            return self.getreply()

    return _ObservedSMTPSSL(host, port, timeout=timeout)


def _email_failure_state(error: Exception, phase: str, client) -> str:
    """Decide whether an SMTP failure may be retried automatically.

    'retry' is only returned when it is provable that no message content could
    have been accepted:

      * the failure happened before ``send_message()`` was entered — connect,
        EHLO, TLS handshake or AUTH;
      * the server explicitly refused the message (``SMTPDataError`` carries the
        reply code, which is positive evidence of non-acceptance);
      * the observing client proves the DATA command never received 354.

    Every other outcome stays 'uncertain'. Retrying an ambiguous send risks
    duplicate mail, and that is never an acceptable trade for availability.
    """
    if isinstance(error, smtplib.SMTPDataError):
        return "retry"
    if phase != "send":
        return "retry"
    committed = getattr(client, "content_committed", None) if client is not None else None
    if committed is False:
        return "retry"
    return "uncertain"


def send_paid_order_email(order_id: int):
    """Durable SMTP attempt; ambiguous outcomes require manual reconciliation."""
    order = get_order_by_id(order_id)
    if not order or order.get("payment_status") != "paid":
        return False

    # Idempotency guard: skip if email was already successfully sent
    if order.get("payment_email_sent_at"):
        logging.info("Email об оплате заказа #%s уже был отправлен ранее, пропуск.", order_id)
        return True

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
        return False

    msg = MIMEMultipart()
    msg["From"] = smtp_user
    msg["To"] = email_to
    msg["Subject"] = f"✅ Заказ #{order_id} ОПЛАЧЕН ({order.get('payment_amount', '0.00')} руб.) | PhotoDoc AI"
    msg["Message-ID"] = message_id(order_id, "paid")
    body = (
        f"Заказ #{order_id} успешно оплачен!\n"
        f"Клиент: {order.get('name')}\n"
        f"Телефон: {order.get('phone')}\n"
        f"Сумма оплаты: {order.get('payment_amount')} руб.\n"
        f"Дата оплаты: {order.get('paid_at')}\n"
    )
    msg.attach(MIMEText(body, "plain"))

    paid_email_outbox.initialize(DB_PATH)
    action, attempt = paid_email_outbox.claim(DB_PATH, order_id)
    if action != "send":
        return action == "sent"
    payment_phase = "connect"
    client = None
    accepted = False
    acknowledged = False
    try:
        with _smtp_transport(smtp_server, smtp_port, 30) as server:
            client = server
            payment_phase = "auth"
            server.login(smtp_user, smtp_password)
            require_paid_order(order_id)
            payment_phase = "send"
            refused = server.send_message(msg)
            if isinstance(refused, dict) and refused:
                # At least one recipient accepted and the message received its
                # final reply, so it was delivered and must never be resent.
                logging.warning(
                    "Paid email %s: %s recipient(s) refused: %s",
                    order_id, len(refused), sorted(refused),
                )
            accepted = True
            # QUIT failure must not undo a successful DATA acknowledgment.
            paid_email_outbox.record(DB_PATH, order_id, attempt, "sent")
            acknowledged = True
    except Exception as error:
        if accepted:
            # record() retries SQL only. Durable 'sending' blocks retransmission
            # if acknowledgment still cannot be committed after bounded retries.
            logging.warning("Paid email %s acknowledgment: %s", order_id, type(error).__name__)
            return acknowledged
        state = _email_failure_state(error, payment_phase, client)
        paid_email_outbox.record(DB_PATH, order_id, attempt, state, type(error).__name__)
        return False
    return True


def require_paid_order(order_id: int) -> None:
    order = get_order_by_id(order_id)
    if not order or order.get("payment_status") != "paid":
        raise RuntimeError("Delivery requires a paid order")


def _validated_yandex_destination(path: str) -> str:
    path = _normalize_yandex_path(path)
    # Explicitly configured namespaces must not be escaped by persisted jobs or
    # callers. Legacy callers without an explicit root keep relative Disk paths.
    if (os.getenv("YANDEX_DISK_ROOT_DIR") or "").strip():
        if not _within_yandex_root(path, get_yandex_disk_root()):
            raise RuntimeError("Yandex destination is outside the configured root")
    return path


def ensure_yandex_folder(token: str, path: str):
    path = _validated_yandex_destination(path)
    headers = {"Authorization": f"OAuth {token}"}
    prefix, parts = _split_yandex_path(path)

    def exists(directory: str) -> bool:
        metadata = httpx.get(
            "https://cloud-api.yandex.net/v1/disk/resources",
            headers=headers, params={"path": directory, "fields": "type"}, timeout=10,
        )
        if metadata.status_code == 404:
            return False
        metadata.raise_for_status()
        if metadata.status_code != 200:
            raise RuntimeError("Yandex directory lookup was not confirmed")
        try:
            resource = metadata.json()
        except (ValueError, TypeError):
            raise RuntimeError("Yandex directory metadata is invalid") from None
        if not isinstance(resource, dict) or resource.get("type") != "dir":
            raise RuntimeError("Yandex directory path is not a directory")
        return True

    # Virtual roots are lookup-only; never attempt to mkdir 'app:' or 'disk:'.
    if prefix and not exists(prefix):
        raise RuntimeError("Yandex namespace root is unavailable")
    for index in range(1, len(parts) + 1):
        current_path = prefix + "/".join(parts[:index])
        if exists(current_path):
            continue
        response = httpx.put(
            "https://cloud-api.yandex.net/v1/disk/resources",
            headers=headers,
            params={"path": current_path},
            timeout=10,
        )
        if response.status_code == 201:
            continue
        if response.status_code == 409:
            # Another worker may have created it after our GET. A conflict with
            # a file (or a disappearing directory) is not successful mkdir.
            if exists(current_path):
                continue
            raise RuntimeError("Conflicting Yandex directory was not confirmed")
        response.raise_for_status()
        raise RuntimeError("Yandex folder creation was not confirmed")


def _remote_verified(
    token: str,
    remote_path: str,
    expected_sha256: str | None,
    expected_size: int | None,
) -> bool:
    if not token or not remote_path or not expected_sha256 or expected_size is None:
        raise RuntimeError("Missing required parameters for remote verification")
    remote_path = _validated_yandex_destination(remote_path)
    response = httpx.get(
        "https://cloud-api.yandex.net/v1/disk/resources",
        headers={"Authorization": f"OAuth {token}"},
        params={"path": remote_path, "fields": "sha256,size,type"},
        timeout=15,
    )
    if response.status_code == 404:
        return False
    response.raise_for_status()
    try:
        metadata = response.json()
    except Exception as exc:
        raise RuntimeError(f"Remote metadata is not valid JSON: {exc}")
    if not isinstance(metadata, dict):
        raise RuntimeError("Remote metadata is not a JSON object")
    if metadata.get("type") != "file":
        raise RuntimeError(f"Remote resource is not a file: type={metadata.get('type')!r}")
    actual_sha = metadata.get("sha256")
    if not isinstance(actual_sha, str) or not actual_sha:
        raise RuntimeError("Remote resource metadata is missing sha256 checksum")
    if actual_sha.lower() != expected_sha256.lower():
        raise RuntimeError(f"Remote file checksum does not match: actual={actual_sha.lower()} expected={expected_sha256.lower()}")
    actual_size = metadata.get("size")
    if not isinstance(actual_size, int):
        raise RuntimeError("Remote resource metadata is missing size")
    if actual_size != expected_size:
        raise RuntimeError(f"Remote file size does not match: actual={actual_size} expected={expected_size}")
    return True


def upload_to_yandex_disk(
    local_file_path: str,
    remote_folder_path: str,
    remote_file_name: str,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
    uploads_dir: Path | None = None,
    order_id: int | None = None,
):
    if order_id is not None:
        require_paid_order(order_id)
    token = os.getenv("YANDEX_DISK_TOKEN")
    if not token:
        raise RuntimeError("Yandex Disk OAuth token is unavailable")
    remote_folder_path = _normalize_yandex_path(remote_folder_path)
    remote_path = _join_yandex_path(remote_folder_path, remote_file_name)

    if is_staging_environment():
        if is_production_disk_path(remote_folder_path):
            raise RuntimeError(
                f"Staging delivery cannot upload to production destination: {remote_folder_path}"
            )
        token_lower = token.lower()
        if (
            token_lower in KNOWN_STAGING_PLACEHOLDERS
            or "placeholder" in token_lower
            or "example" in token_lower
            or token_lower.endswith("_mock")
        ):
            if os.getenv("PHOTODOC_TEST_MOCK_TOKEN_ALLOWED", "0") != "1":
                raise RuntimeError(
                    "Staging delivery requires explicit genuine staging token (placeholder rejected)"
                )

    remote_folder_path = _validated_yandex_destination(remote_folder_path)
    raw_path = str(local_file_path)
    if ".." in raw_path or any(part == ".." for part in Path(raw_path).parts):
        raise RuntimeError("Path traversal detected in upload source")

    local = Path(raw_path)
    if local.is_symlink():
        raise RuntimeError("Symlinked source file rejected")

    try:
        resolved_local = local.resolve()
    except Exception as exc:
        raise RuntimeError(f"Failed to resolve upload source: {exc}")

    if resolved_local.is_symlink():
        raise RuntimeError("Symlinked source file rejected")

    if uploads_dir is not None:
        uploads_resolved = uploads_dir.resolve()
        if uploads_resolved not in resolved_local.parents and resolved_local.parent != uploads_resolved:
            raise RuntimeError("Local delivery source is outside uploads")

    if not local.is_file():
        raise RuntimeError(f"Local delivery source is missing: {local_file_path}")

    # Validate local file identity against expected before initiating upload
    digest = hashlib.sha256()
    size = 0
    with local.open("rb") as source:
        for chunk in iter(lambda: source.read(65536), b""):
            digest.update(chunk)
            size += len(chunk)

    local_sha256 = digest.hexdigest().lower()
    if expected_sha256 and local_sha256 != expected_sha256.lower():
        raise RuntimeError("Local delivery source checksum does not match delivery_jobs record")
    if expected_size is not None and size != expected_size:
        raise RuntimeError("Local delivery source size does not match delivery_jobs record")

    target_sha256 = (expected_sha256 or local_sha256).lower()
    target_size = expected_size if expected_size is not None else size

    # If authoritative expected checksum and size are known, check if already verified remotely
    if expected_sha256 and expected_size is not None:
        if _remote_verified(token, remote_path, expected_sha256, expected_size):
            return True

    ensure_yandex_folder(token, remote_folder_path)
    headers = {"Authorization": f"OAuth {token}"}

    if _remote_verified(token, remote_path, target_sha256, target_size):
        return True

    upload_url_response = httpx.get(
        "https://cloud-api.yandex.net/v1/disk/resources/upload",
        headers=headers,
        params={"path": remote_path, "overwrite": "false"},
        timeout=30,
    )
    if upload_url_response.status_code == 409 and _remote_verified(token, remote_path, target_sha256, target_size):
        return True
    upload_url_response.raise_for_status()
    href = upload_url_response.json().get("href")
    if not href or not href.startswith("https://"):
        raise RuntimeError("Yandex upload URL is missing or invalid")

    with local.open("rb") as source:
        def chunks():
            for chunk in iter(lambda: source.read(65536), b""):
                yield chunk
        if order_id is not None:
            require_paid_order(order_id)
        put_response = httpx.put(
            href,
            content=chunks(),
            headers={"Content-Length": str(target_size)},
            timeout=httpx.Timeout(120.0, connect=30.0),
        )
        put_response.raise_for_status()

    # Step 4 & 5: Verify remote metadata matches delivery_jobs authoritative sha256 and size
    if not _remote_verified(token, remote_path, target_sha256, target_size):
        raise RuntimeError("Yandex upload was not confirmed by resource metadata")
    return True


def order_info_bytes(order_id: int, name: str, phone: str, format: str, paper: str, crop: str, comment: str) -> bytes:
    order_info_text = (
        f"Заказ №: {order_id}\nИмя: {name}\nТелефон: {phone}\n"
        f"Формат: {format}\nБумага: {paper}\nКадрирование: {crop}\nКомментарий: {comment}"
    )
    return order_info_text.encode("utf-8")


def upload_info_to_yandex_disk(
    order_id: int,
    name: str,
    phone: str,
    format: str,
    paper: str,
    crop: str,
    comment: str,
    remote_folder_path: str,
    source_path: str | None = None,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
    uploads_dir: Path | None = None,
):
    if source_path:
        return upload_to_yandex_disk(
            source_path,
            remote_folder_path,
            "info.txt",
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            uploads_dir=uploads_dir,
            order_id=order_id,
        )
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "info.txt"
        info_bytes = order_info_bytes(order_id, name, phone, format, paper, crop, comment)
        source.write_bytes(info_bytes)
        return upload_to_yandex_disk(
            str(source),
            remote_folder_path,
            "info.txt",
            expected_sha256=expected_sha256 or hashlib.sha256(info_bytes).hexdigest(),
            expected_size=expected_size or len(info_bytes),
            order_id=order_id,
        )


def deliver_job(job: dict) -> None:
    order = get_order_by_id(job["order_id"])
    if not order:
        raise RuntimeError("Order for delivery job is missing")
    if job["kind"] in ("file", "info"):
        require_paid_order(job["order_id"])
        raw_path = job.get("local_path")
        if not raw_path:
            raise RuntimeError("Delivery job is missing local_path")
        if ".." in raw_path or any(part == ".." for part in Path(raw_path).parts):
            raise RuntimeError("Path traversal detected in local_path")
        path = Path(raw_path)
        if path.is_symlink():
            raise RuntimeError("Symlinked source file rejected")

        # Isolation: file must belong to this order
        if not path.name.startswith(f"{job['order_id']}_"):
            raise RuntimeError("Local file name does not match order ID")

        try:
            target_resolved = path.resolve()
        except Exception as exc:
            raise RuntimeError(f"Failed to resolve delivery source path: {exc}")
        if target_resolved.is_symlink():
            raise RuntimeError("Symlinked source file rejected")

        upload_resolved = UPLOAD_DIR.resolve()
        if upload_resolved not in target_resolved.parents and target_resolved.parent != upload_resolved:
            raise RuntimeError("Local delivery source is outside uploads")

        remote_folder, remote_name = job["remote_path"].rsplit("/", 1)
        expected_sha256 = job.get("content_sha256")
        expected_size = job.get("content_size")
        if job["kind"] == "info":
            ok = upload_info_to_yandex_disk(
                order["id"], order["name"], order["phone"], order["format"],
                order["paper"], order["crop"], order["comment"], remote_folder,
                source_path=str(path),
                expected_sha256=expected_sha256,
                expected_size=expected_size,
                uploads_dir=UPLOAD_DIR,
            )
        else:
            ok = upload_to_yandex_disk(
                str(path),
                remote_folder,
                remote_name,
                expected_sha256=expected_sha256,
                expected_size=expected_size,
                uploads_dir=UPLOAD_DIR,
                order_id=job["order_id"],
            )
    elif job["kind"] == "admin_email":
        ok = send_order_email(order["id"], order["name"], order["phone"], order["comment"],
                              order["format"], order["paper"], order["crop"])
    elif job["kind"] == "paid_email":
        ok = send_paid_order_email(order["id"])
    else:
        raise RuntimeError("Unknown delivery job kind")
    if not ok:
        raise RuntimeError("Delivery was not confirmed")


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
    if not 1 <= len(files) <= MAX_FILES:
        raise HTTPException(status_code=400, detail="Invalid number of files")
    amount = format_robokassa_amount(calculate_authoritative_price(format, paper, crop, len(files)))
    staged = []
    finalized = []
    conn = None
    commit_started = False
    try:
        total = 0
        for file in files:
            item = await _stage_upload(file, total)
            staged.append(item)
            total += item["size"]

        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "INSERT INTO orders (name, phone, email, format, paper, crop, comment, filename, "
            "status, payment_status, order_amount, payment_amount, created_at, order_token_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', 'unpaid', ?, ?, ?, ?)",
            (name.strip(), phone.strip(), email.strip(), format.strip(), paper.strip(), crop.strip(),
             comment.strip(), ", ".join(sanitize_upload_filename(x["original_name"]) for x in staged),
             amount, amount, datetime.utcnow().isoformat(), token_hash),
        )
        order_id = cursor.lastrowid
        folder = _join_yandex_path(
            get_yandex_disk_root(), f"Заказ_{order_id}_{_folder_component(name)}",
            f"{_folder_component(format)}_{_folder_component(paper)}_{_folder_component(crop)}",
        )
        for item in staged:
            attachment_id = uuid.uuid4().hex
            stored_name = f"{order_id}_{attachment_id}.{item['suffix']}"
            final_path = UPLOAD_DIR / stored_name
            item["path"].replace(final_path)
            finalized.append(final_path)
            enqueue(conn, order_id=order_id, kind="file", local_path=str(final_path),
                    original_name=item["original_name"], remote_path=f"{folder}/{stored_name}",
                    sha256=item["sha256"], size=item["size"])

        info_path = UPLOAD_DIR / f"{order_id}_{uuid.uuid4().hex}_info.txt"
        info = order_info_bytes(order_id, name, phone, format, paper, crop, comment)
        with info_path.open("xb") as output:
            output.write(info)
            output.flush()
            os.fsync(output.fileno())
        finalized.append(info_path)
        enqueue(conn, order_id=order_id, kind="info", local_path=str(info_path),
                original_name="info.txt", remote_path=f"{folder}/info.txt",
                sha256=hashlib.sha256(info).hexdigest(), size=len(info))
        enqueue(conn, order_id=order_id, kind="admin_email")
        commit_started = True
        conn.commit()
        return {"ok": True, "message": "Заказ успешно создан", "order_id": order_id,
                "order_token": token, "amount": amount}
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        for item in staged:
            item["path"].unlink(missing_ok=True)
        if not commit_started:
            for path in finalized:
                path.unlink(missing_ok=True)
        if isinstance(exc, HTTPException):
            raise
        logging.error("Критическая ошибка при создании заказа: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера") from exc
    finally:
        if conn is not None:
            conn.close()


# --- Эндпоинты платежной системы Robokassa ---
class PaymentCreateRequest(BaseModel):
    order_id: int | str
    order_token: str
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
    stored_token_hash = order.get("order_token_hash")
    if not stored_token_hash:
        raise HTTPException(status_code=409, detail="Legacy order requires manual payment handling")
    supplied_hash = hashlib.sha256(payment.order_token.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(stored_token_hash, supplied_hash):
        raise HTTPException(status_code=404, detail="Order not found")
    if order.get("payment_status") == "paid":
        raise HTTPException(status_code=409, detail="Order is already paid")
    if order.get("payment_status") not in {"unpaid", "pending"}:
        raise HTTPException(status_code=409, detail="Order payment state is invalid")

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
    try:
        transition_result = update_order_payment_pending(order_id_int, amount_to_pay)
    except PaymentStateError:
        raise HTTPException(status_code=409, detail="Order payment state is invalid")
    if transition_result == "paid":
        raise HTTPException(status_code=409, detail="Order is already paid")
    if transition_result == "not_found":
        raise HTTPException(status_code=404, detail="Order not found")

    order_id_str = str(order_id_int)
    customer_email = (order.get("email") or "").strip()
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
        # M-05: never log the raw callback — a sender controls those fields and may embed
        # contact data, tokens or signatures. Only the field names are safe.
        logging.error(
            "Result URL: отсутствуют обязательные параметры; получены поля: %s",
            sorted(payload.keys()),
        )
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
    try:
        received_amount = format_robokassa_amount(out_sum)
    except (ValueError, OverflowError):
        raise HTTPException(status_code=400, detail="Invalid OutSum") from None
    if received_amount != expected_amount:
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
        # The paid-email job was committed in the same transaction as the paid state.
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

# ---------------------------------------------------------------------------
# Operator workflow (contract PHOTODOC-R1-OPERATOR-CONTRACT 1.0).
# Every route authenticates independently; without OPERATOR_TOKEN the whole
# surface answers 503 and nothing is reachable.
# ---------------------------------------------------------------------------

def _require_operator(request: Request) -> None:
    operator_service.require_operator(request.headers.get("authorization"))


@app.get("/api/operator/orders")
def operator_orders(request: Request):
    _require_operator(request)
    return {"ok": True, "orders": operator_service.list_paid_orders(DB_PATH)}


@app.get("/api/operator/orders/{order_id}")
def operator_order(order_id: int, request: Request):
    _require_operator(request)
    return {"ok": True, "order": operator_service.order_metadata(DB_PATH, order_id)}


@app.get("/api/operator/orders/{order_id}/source")
def operator_source(order_id: int, request: Request):
    _require_operator(request)
    path = operator_service.source_for_download(DB_PATH, order_id)
    # M-03: a protected photograph must never be cached by a browser or a proxy.
    return FileResponse(path, media_type="application/octet-stream", filename=path.name,
                        headers={"Cache-Control": "no-store, private"})


@app.post("/api/operator/orders/{order_id}/result")
async def operator_upload_result(order_id: int, request: Request,
                                 file: UploadFile = File(...), replace: bool = False):
    _require_operator(request)
    payload = operator_service.read_limited(file)      # M-06: bounded read
    suffix, sha256 = operator_service.validate_result(file, payload)
    stored = operator_service.store_result(DB_PATH, order_id, suffix, payload, sha256, replace=replace)
    return {"ok": True, "order_id": order_id, "bytes": len(payload), "sha256": sha256[:16]}


@app.post("/api/operator/orders/{order_id}/submit")
def operator_submit(order_id: int, request: Request):
    _require_operator(request)
    job_id = operator_service.submit_delivery(DB_PATH, order_id)
    return {"ok": True, "order_id": order_id, "job_id": job_id}


@app.get("/api/operator/orders/{order_id}/delivery")
def operator_delivery(order_id: int, request: Request):
    _require_operator(request)
    return {"ok": True, "order_id": order_id,
            "jobs": operator_service.delivery_state(DB_PATH, order_id)}
