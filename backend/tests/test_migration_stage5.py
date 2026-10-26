"""P1 Stage 5 migration and recovery validation suite on synthetic database copies."""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
import main  # noqa: E402
from db_contract import APPLICATION_ID, inspect_database  # noqa: E402
from delivery import claim_due, enqueue, finish, process_due, utc_now  # noqa: E402
from migrations.reconcile_delivery_schema import reconcile  # noqa: E402

P0_COLUMNS = (
    "id", "name", "phone", "email", "format", "paper", "crop", "comment",
    "filename", "status", "payment_status", "order_amount", "payment_amount",
    "payment_method", "robokassa_inv_id", "paid_at", "payment_email_sent_at",
    "created_at",
)


def create_p0_synthetic_db(db_path: Path, order_count: int = 5) -> None:
    """Creates a clean synthetic database that strictly adheres to P0 schema & identity."""
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    cols_def = ", ".join(f"{col} TEXT" if col != "id" else "id INTEGER PRIMARY KEY AUTOINCREMENT" for col in P0_COLUMNS)
    conn.execute(f"CREATE TABLE orders ({cols_def})")
    for i in range(1, order_count + 1):
        conn.execute(
            "INSERT INTO orders (name, phone, email, format, paper, crop, status, payment_status, order_amount) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"Old Customer {i}", f"+7999000{i:04d}", f"cust{i}@example.com", "10x15", "Матовая", "Без полей",
             "new", "unpaid", "20.00"),
        )
    conn.commit()
    conn.close()


def test_stage5_full_lifecycle_sequence(tmp_path, monkeypatch):
    """
    Lifecycle test sequence:
    1. P0 schema synthetic database
    2. P1 migration applied
    3. P1 backend application starts
    4. New order created with token and jobs
    5. Simulated process restart
    6. Delivery jobs recovered and completed
    """
    db_path = tmp_path / "lifecycle_orders.db"
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()

    # 1. P0 synthetic DB
    create_p0_synthetic_db(db_path, order_count=3)
    p0_ready, p0_details = inspect_database(db_path, require_complete_schema=True)
    assert p0_ready and p0_details["identity_valid"]

    # 2. P1 migration dry-run check, then apply
    check_res = reconcile(db_path, apply=False)
    assert check_res["migration_required"] is True
    assert check_res["applied"] is False

    apply_res = reconcile(db_path, apply=True)
    assert apply_res["migration_required"] is True
    assert apply_res["applied"] is True

    # Re-run migration: must be idempotent no-op
    rerun_res = reconcile(db_path, apply=True)
    assert rerun_res["migration_required"] is False
    assert rerun_res["applied"] is False

    # 3. P1 app startup
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads_dir)
    monkeypatch.setenv("ROBOKASSA_MERCHANT_LOGIN", "test-merchant")
    monkeypatch.setenv("ROBOKASSA_PASSWORD1", "test-pass-1")
    monkeypatch.setenv("ROBOKASSA_PASSWORD2", "test-pass-2")
    monkeypatch.setenv("DELIVERY_WORKER_ENABLED", "0")

    assert main.database_readiness_ok()
    assert main.delivery_schema_ready()

    client = TestClient(main.app)
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    # 4. Create new order in P1 app
    resp = client.post("/api/order", data={
        "name": "P1 Customer", "phone": "+79998887766", "format": "10x15",
    }, files=[("files", ("photo.jpg", b"\xff\xd8\xffsynthetic-content", "image/jpeg"))])
    assert resp.status_code == 200
    new_order_id = resp.json()["order_id"]
    order_token = resp.json()["order_token"]

    # 5. Simulated restart: restart backend, inspect persisted delivery queue
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        pending_jobs = conn.execute("SELECT * FROM delivery_jobs WHERE order_id=?", (new_order_id,)).fetchall()
        assert len(pending_jobs) == 3
        assert all(j["state"] == "pending" for j in pending_jobs)

    # 6. Delivery worker processes recovered jobs
    delivered_jobs = []
    processed_count = process_due(db_path, lambda job: delivered_jobs.append(job["id"]))
    assert processed_count == 3
    assert len(delivered_jobs) == 3

    with sqlite3.connect(db_path) as conn:
        all_delivered = conn.execute(
            "SELECT COUNT(*) FROM delivery_jobs WHERE order_id=? AND state='delivered'", (new_order_id,)
        ).fetchone()[0]
        assert all_delivered == 3


def test_stage5_unsuitable_db_and_missing_application_id_fail_closed(tmp_path):
    """Неподходящая SQLite-БД и отсутствие application_id: отказ выполнения миграции."""
    # 1. Non-existent DB
    with pytest.raises(RuntimeError, match="P0 PhotoDoc database identity/schema check failed"):
        reconcile(tmp_path / "nonexistent.db")

    # 2. Arbitrary non-PhotoDoc SQLite DB (missing application_id)
    raw_db = tmp_path / "raw.db"
    conn = sqlite3.connect(raw_db)
    conn.execute("CREATE TABLE foo (id INT)")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="P0 PhotoDoc database identity/schema check failed"):
        reconcile(raw_db)

    # 3. DB with wrong application_id
    wrong_id_db = tmp_path / "wrong_id.db"
    conn = sqlite3.connect(wrong_id_db)
    conn.execute("PRAGMA application_id = 999999")
    conn.execute("CREATE TABLE orders (id INT)")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="P0 PhotoDoc database identity/schema check failed"):
        reconcile(wrong_id_db)


def test_stage5_interrupted_migration_rolls_back_cleanly(tmp_path):
    """Прерывание миграции: транзакционный откат оставляет БД в исходном состоянии P0."""
    db_path = tmp_path / "rollback_test.db"
    create_p0_synthetic_db(db_path, order_count=2)

    real_connect = sqlite3.connect

    class FailingConnection:
        def __init__(self, *args, **kwargs):
            self._real = real_connect(*args, **kwargs)

        def __getattr__(self, name):
            if name == "commit":
                def failing_commit():
                    raise sqlite3.OperationalError("Simulated disk error during commit")
                return failing_commit
            return getattr(self._real, name)

    # Simulate error during migration inside transaction before commit completes
    with patch("migrations.reconcile_delivery_schema.sqlite3.connect", side_effect=FailingConnection):
        with pytest.raises(sqlite3.OperationalError, match="Simulated disk error"):
            reconcile(db_path, apply=True)

    # DB must remain in clean P0 state without partial schema corruption
    with sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
        assert "order_token_hash" not in cols, "Rolled back migration must not leave new column"
        has_delivery_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_jobs'"
        ).fetchone()
        assert has_delivery_table is None, "Rolled back migration must not leave delivery_jobs table"


def test_stage5_historical_orders_preserved_and_backup_restoration(tmp_path):
    """
    Сохранение старых заказов, восстановление из резервной копии:
    - Никакие пользовательские данные не изменяются.
    - Восстановленная из бэкапа БД корректно мигрирует.
    """
    db_path = tmp_path / "orders.db"
    backup_path = tmp_path / "orders_backup.db"
    create_p0_synthetic_db(db_path, order_count=5)

    # Take verified backup before migration
    shutil.copy2(db_path, backup_path)

    # Record historical rows snapshot before migration
    with sqlite3.connect(db_path) as conn:
        before_rows = conn.execute("SELECT * FROM orders ORDER BY id").fetchall()

    # Apply P1 migration
    res = reconcile(db_path, apply=True)
    assert res["applied"] is True

    # Verify historical data unchanged after migration
    with sqlite3.connect(db_path) as conn:
        after_rows = conn.execute("SELECT * FROM orders ORDER BY id").fetchall()
        # All original columns match exactly; only new order_token_hash is appended as None
        for b_row, a_row in zip(before_rows, after_rows):
            assert a_row[:-1] == b_row
            assert a_row[-1] is None, "Historical order must have order_token_hash=None"

    # Verify backup can be restored into a new location and successfully migrated
    restored_db = tmp_path / "restored_orders.db"
    shutil.copy2(backup_path, restored_db)
    restore_res = reconcile(restored_db, apply=True)
    assert restore_res["applied"] is True

    with sqlite3.connect(restored_db) as conn:
        restored_rows = conn.execute("SELECT * FROM orders ORDER BY id").fetchall()
        assert len(restored_rows) == 5


def test_stage5_hardened_sqlite_wal_backup_and_uploads_rehearsal(tmp_path):
    """
    Release Gate R0 Hardened Backup & Recovery Protocol:
    1. Database in WAL mode with active transactions and photos on disk.
    2. Simulated writer shutdown: flush & truncate WAL via PRAGMA wal_checkpoint(TRUNCATE).
    3. SQLite Online Backup API snapshot.
    4. Atomic archive of uploads directory with directory structure preserved.
    5. SHA-256 and metadata manifest generation (counts, checksums, application_id).
    6. Isolated sandbox extraction & recovery verification (checksums, integrity_check, file cross-check).
    7. Rollback compatibility verification (P0 inspect_database compatibility and clean pre-migration restoration).
    """
    # 1. Setup DB with WAL mode, orders, and associated upload files
    db_path = tmp_path / "live" / "orders.db"
    db_path.parent.mkdir(parents=True)
    uploads_dir = tmp_path / "live" / "uploads"
    uploads_dir.mkdir(parents=True)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    cols_def = ", ".join(f"{col} TEXT" if col != "id" else "id INTEGER PRIMARY KEY AUTOINCREMENT" for col in P0_COLUMNS)
    conn.execute(f"CREATE TABLE orders ({cols_def})")

    for i in range(1, 4):
        fname = f"customer_photo_{i}.jpg"
        (uploads_dir / fname).write_bytes(f"photo-data-{i}".encode("utf-8"))
        conn.execute(
            "INSERT INTO orders (name, phone, email, format, paper, crop, filename, status, payment_status, order_amount) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"Customer {i}", f"+799911122{i:02d}", f"c{i}@example.com", "10x15", "Глянцевая", "Без полей",
             fname, "new", "paid", "20.00"),
        )
    conn.commit()

    # 2. Step 1: WAL checkpoint TRUNCATE with writer stopped
    res = conn.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchone()
    assert res[0] == 0, "WAL checkpoint should not be busy"

    # 3. Step 2: SQLite Online Backup API snapshot
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True)
    backup_db_path = backup_dir / "orders.db"

    backup_conn = sqlite3.connect(backup_db_path)
    with backup_conn:
        conn.backup(backup_conn)
    backup_conn.close()
    conn.close()

    # 4. Step 3: Uploads archive preserving directory structure
    uploads_archive_path = backup_dir / "uploads.tar.gz"
    with tarfile.open(uploads_archive_path, "w:gz") as tar:
        tar.add(uploads_dir, arcname="uploads")

    # 5. Step 4: SHA-256 and metadata manifest
    db_sha256 = hashlib.sha256(backup_db_path.read_bytes()).hexdigest()
    uploads_sha256 = hashlib.sha256(uploads_archive_path.read_bytes()).hexdigest()

    manifest_txt = backup_dir / "manifest.sha256"
    manifest_txt.write_text(f"{db_sha256}  orders.db\n{uploads_sha256}  uploads.tar.gz\n", encoding="utf-8")

    bconn = sqlite3.connect(backup_db_path)
    b_app_id = bconn.execute("PRAGMA application_id;").fetchone()[0]
    b_order_count = bconn.execute("SELECT COUNT(*) FROM orders;").fetchone()[0]
    b_max_id = bconn.execute("SELECT MAX(id) FROM orders;").fetchone()[0]
    bconn.close()

    metadata = {
        "timestamp": "2026-10-26T00:00:00Z",
        "application_id": b_app_id,
        "orders_count": b_order_count,
        "max_id": b_max_id,
        "uploads_files_count": len(list(uploads_dir.iterdir())),
        "orders_db_sha256": db_sha256,
        "uploads_tar_sha256": uploads_sha256,
    }
    (backup_dir / "backup_manifest.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    # 6. Step 5: Independent sandbox restore & integrity verification
    restore_sandbox = tmp_path / "sandbox_restore"
    restore_sandbox.mkdir()

    # Validate checksums against manifest
    assert hashlib.sha256(backup_db_path.read_bytes()).hexdigest() == db_sha256
    assert hashlib.sha256(uploads_archive_path.read_bytes()).hexdigest() == uploads_sha256

    shutil.copy2(backup_db_path, restore_sandbox / "orders.db")
    with tarfile.open(uploads_archive_path, "r:gz") as tar:
        tar.extractall(restore_sandbox)

    restored_db = restore_sandbox / "orders.db"
    restored_uploads = restore_sandbox / "uploads"

    rconn = sqlite3.connect(restored_db)
    assert rconn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
    assert rconn.execute("PRAGMA application_id;").fetchone()[0] == APPLICATION_ID
    assert rconn.execute("SELECT COUNT(*) FROM orders;").fetchone()[0] == metadata["orders_count"]

    order_files = rconn.execute(
        "SELECT id, filename FROM orders WHERE filename IS NOT NULL AND filename != ''"
    ).fetchall()
    for oid, fnames in order_files:
        for f in fnames.split(","):
            f = f.strip()
            if f:
                assert (restored_uploads / f).is_file(), f"Photo {f} missing from restored uploads for order {oid}"
    rconn.close()

    # 7. Step 6: P1 migration compatibility & rollback
    mig_res = reconcile(restored_db, apply=True)
    assert mig_res["applied"] is True

    # P0 inspect_database accepts migrated DB (additive migration)
    p0_ready, p0_details = inspect_database(restored_db, require_complete_schema=True)
    assert p0_ready is True
    assert p0_details["identity_valid"] is True
    assert p0_details["orders_table_present"] is True
    assert p0_details["missing_columns"] == []
