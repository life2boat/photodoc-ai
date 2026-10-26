"""P1 upload limit tests matching Stage 3 requirements."""
import hashlib
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
import main  # noqa: E402

JPEG_HEADER = b"\xff\xd8\xff" + b"\x00" * 10
PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10
WEBP_HEADER = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 10


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    db = tmp_path / "orders.db"
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(main, "DB_PATH", db)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)
    monkeypatch.setenv("ROBOKASSA_MERCHANT_LOGIN", "test-merchant")
    monkeypatch.setenv("ROBOKASSA_PASSWORD1", "test-pass-1")
    monkeypatch.setenv("ROBOKASSA_PASSWORD2", "test-pass-2")
    monkeypatch.setenv("DELIVERY_WORKER_ENABLED", "0")
    main.init_db()
    yield TestClient(main.app), db, uploads


def test_1_small_file_succeeds(isolated):
    """1. Маленький файл: корректная загрузка и создание заказа."""
    client, db, uploads = isolated
    small_data = JPEG_HEADER + b"small-content-1024-bytes" * 40
    resp = client.post("/api/order", data={
        "name": "Small Client", "phone": "79990001122", "format": "10x15",
    }, files=[("files", ("small.jpg", small_data, "image/jpeg"))])
    assert resp.status_code == 200, resp.text
    order_id = resp.json()["order_id"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders WHERE id=?", (order_id,)).fetchone()[0] == 1
        jobs = conn.execute("SELECT kind, local_path FROM delivery_jobs WHERE order_id=?", (order_id,)).fetchall()
        assert len(jobs) == 3
        file_jobs = [j for j in jobs if j[0] == "file"]
        assert len(file_jobs) == 1
        assert Path(file_jobs[0][1]).is_file()


def test_2_duplicate_file_names_preserved_distinctly(isolated):
    """2. Несколько файлов с одинаковыми именами: уникальные имена файлов и сохранение оригинала."""
    client, db, uploads = isolated
    file_a = JPEG_HEADER + b"data-for-duplicate-a"
    file_b = JPEG_HEADER + b"data-for-duplicate-b"
    resp = client.post("/api/order", data={
        "name": "Dup Client", "phone": "79990002233", "format": "10x15",
    }, files=[
        ("files", ("photo.jpg", file_a, "image/jpeg")),
        ("files", ("photo.jpg", file_b, "image/jpeg")),
    ])
    assert resp.status_code == 200, resp.text
    order_id = resp.json()["order_id"]
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        file_jobs = conn.execute("SELECT * FROM delivery_jobs WHERE order_id=? AND kind='file'", (order_id,)).fetchall()
        assert len(file_jobs) == 2
        assert file_jobs[0]["original_name"] == "photo.jpg"
        assert file_jobs[1]["original_name"] == "photo.jpg"
        assert file_jobs[0]["local_path"] != file_jobs[1]["local_path"]
        assert file_jobs[0]["remote_path"] != file_jobs[1]["remote_path"]
        assert Path(file_jobs[0]["local_path"]).is_file()
        assert Path(file_jobs[1]["local_path"]).is_file()


def test_3_order_over_50mib_within_80mib_accepted(isolated):
    """3. Заказ общей величиной свыше 50 МиБ, но в пределах лимита (80 МиБ): должен приниматься."""
    client, db, uploads = isolated
    # 3 files of 18 MiB each = 54 MiB (> 50 MiB, < 80 MiB limit, each file < 20 MiB limit)
    chunk = b"\x00" * (1024 * 1024)  # 1 MiB
    eighteen_mb = JPEG_HEADER + chunk * 18

    resp = client.post("/api/order", data={
        "name": "Large Client", "phone": "79990003344", "format": "10x15",
    }, files=[
        ("files", ("part1.jpg", eighteen_mb, "image/jpeg")),
        ("files", ("part2.jpg", eighteen_mb, "image/jpeg")),
        ("files", ("part3.jpg", eighteen_mb, "image/jpeg")),
    ])
    assert resp.status_code == 200, resp.text
    order_id = resp.json()["order_id"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders WHERE id=?", (order_id,)).fetchone()[0] == 1


def test_4_single_file_exceeding_20mib_rejected(isolated):
    """4. Превышение 20 МиБ для одного файла: возврат HTTP 413."""
    client, db, uploads = isolated
    # Mock single file size > MAX_FILE_BYTES (20 MiB)
    with patch.object(main, "MAX_FILE_BYTES", 500):
        oversized = JPEG_HEADER + b"\x00" * 1000
        resp = client.post("/api/order", data={
            "name": "Oversized File", "phone": "79990004455", "format": "10x15",
        }, files=[("files", ("big.jpg", oversized, "image/jpeg"))])
        assert resp.status_code == 413
        assert "limit exceeded" in resp.json()["detail"].lower()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert list(uploads.glob("*big*")) == []


def test_5_order_exceeding_80mib_rejected(isolated):
    """5. Превышение 80 МиБ для заказа: возврат HTTP 413."""
    client, db, uploads = isolated
    # Mock MAX_ORDER_BYTES to 1500 bytes and send 2 x 1000 bytes
    with patch.object(main, "MAX_ORDER_BYTES", 1500):
        file_data = JPEG_HEADER + b"\x00" * 900
        resp = client.post("/api/order", data={
            "name": "Oversized Order", "phone": "79990005566", "format": "10x15",
        }, files=[
            ("files", ("f1.jpg", file_data, "image/jpeg")),
            ("files", ("f2.jpg", file_data, "image/jpeg")),
        ])
        assert resp.status_code == 413
        assert "limit exceeded" in resp.json()["detail"].lower()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert list(uploads.iterdir()) == []


def test_6_exceeding_ten_files_rejected(isolated):
    """6. Превышение десяти файлов: возврат HTTP 400."""
    client, db, uploads = isolated
    file_data = JPEG_HEADER + b"data"
    files = [("files", (f"photo_{i}.jpg", file_data, "image/jpeg")) for i in range(11)]
    resp = client.post("/api/order", data={
        "name": "Too Many Files", "phone": "79990006677", "format": "10x15",
    }, files=files)
    assert resp.status_code == 400
    assert "Invalid number of files" in resp.json()["detail"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert list(uploads.iterdir()) == []


def test_7_invalid_image_signature_rejected(isolated):
    """7. Неверная сигнатура изображения: возврат HTTP 400."""
    client, db, uploads = isolated
    # Claims to be image/jpeg but content is plain text
    resp = client.post("/api/order", data={
        "name": "Bad Magic", "phone": "79990007788", "format": "10x15",
    }, files=[("files", ("fake.jpg", b"NOT_A_JPEG_FILE", "image/jpeg"))])
    assert resp.status_code == 400
    assert "Image content does not match" in resp.json()["detail"]

    # Claims to be WebP but missing WEBP chunk header
    resp_webp = client.post("/api/order", data={
        "name": "Bad WebP", "phone": "79990007788", "format": "10x15",
    }, files=[("files", ("fake.webp", b"RIFF1234CORRUPT", "image/webp"))])
    assert resp_webp.status_code == 400

    # Claims to be executable/pdf
    resp_exe = client.post("/api/order", data={
        "name": "Exe Upload", "phone": "79990007788", "format": "10x15",
    }, files=[("files", ("app.exe", b"MZ\x90\x00", "application/x-msdownload"))])
    assert resp_exe.status_code == 400
    assert "Only JPEG, PNG and WebP" in resp_exe.json()["detail"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert list(uploads.iterdir()) == []


def test_8_aborted_interrupted_upload_leaves_no_orphans(isolated):
    """8. Оборванный HTTP-запрос: удаление временных файлов, отсутствие записей в БД."""
    client, db, uploads = isolated

    # Simulate an I/O error or client disconnect mid-upload
    with patch.object(main, "_stage_upload", side_effect=ConnectionResetError("client disconnected")):
        resp = client.post("/api/order", data={
            "name": "Aborted", "phone": "79990008899", "format": "10x15",
        }, files=[("files", ("file.jpg", JPEG_HEADER, "image/jpeg"))])
        assert resp.status_code == 500

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM delivery_jobs").fetchone()[0] == 0
    # No orphan files left in uploads directory
    assert list(uploads.iterdir()) == []
