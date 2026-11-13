"""Durable SQLite delivery outbox with automated photo cleanup and privacy guards. External I/O is performed outside transactions."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlite_busy import BUSY_TIMEOUT, retry_busy
from paid_email_outbox import EMAIL_SCHEMA

DEFAULT_MAX_ATTEMPTS = int(os.getenv("DELIVERY_MAX_ATTEMPTS", "10"))
DEFAULT_UPLOADS_DIR = Path(os.getenv("UPLOAD_DIR", "/app/uploads"))
_UPLOADS_DIR = DEFAULT_UPLOADS_DIR

ROLE_SOURCE = "source"
ROLE_RESULT = "result"
ROLE_INFO = "info"
ROLE_QUARANTINE = "quarantine"
ROLE_UNKNOWN = "unknown"


def set_uploads_dir(path: Path) -> None:
    global _UPLOADS_DIR
    _UPLOADS_DIR = Path(path)


def get_uploads_dir() -> Path:
    return _UPLOADS_DIR


DELIVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_jobs (
    id TEXT PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    kind TEXT NOT NULL CHECK(kind IN ('file', 'info', 'admin_email', 'paid_email')),
    artifact_role TEXT,
    local_path TEXT,
    original_name TEXT,
    remote_path TEXT,
    content_sha256 TEXT,
    content_size INTEGER,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending', 'processing', 'delivered', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    lease_until TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    cleanup_state TEXT NOT NULL DEFAULT 'pending' CHECK(cleanup_state IN ('pending', 'processing', 'completed', 'failed', 'not_applicable')),
    cleanup_phase TEXT NOT NULL DEFAULT 'none' CHECK(cleanup_phase IN ('none', 'pending', 'deleting', 'deleted', 'failed')),
    local_deleted_at TEXT,
    cleanup_error TEXT,
    cleanup_attempts INTEGER NOT NULL DEFAULT 0,
    cleanup_lease_until TEXT,
    UNIQUE(order_id, kind, remote_path)
);
CREATE INDEX IF NOT EXISTS idx_delivery_due ON delivery_jobs(state, next_attempt_at, lease_until);
CREATE INDEX IF NOT EXISTS idx_cleanup_due ON delivery_jobs(state, cleanup_state, cleanup_lease_until);
CREATE UNIQUE INDEX IF NOT EXISTS idx_paid_email_once ON delivery_jobs(order_id) WHERE kind = 'paid_email';
CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_email_once ON delivery_jobs(order_id) WHERE kind = 'admin_email';
""" + EMAIL_SCHEMA

GATE_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migration_gate (
    gate_name TEXT PRIMARY KEY,
    holder_id TEXT NOT NULL,
    holder_pid INTEGER NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1,
    holder_start_time REAL
);
"""

STREAM_LEASES_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_stream_leases (
    job_id TEXT PRIMARY KEY,
    holder_pid INTEGER NOT NULL,
    holder_start_time REAL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    gate_generation INTEGER NOT NULL DEFAULT 1,
    stream_operation_id TEXT,
    http_completed INTEGER NOT NULL DEFAULT 0,
    delivery_confirmed INTEGER NOT NULL DEFAULT 0
);
"""

DEFAULT_GATE_TIMEOUT_SECONDS = 5.0
DEFAULT_GATE_LEASE_SECONDS = 60
DEFAULT_STREAM_LEASE_SECONDS = 60


def ensure_gate_table(conn: sqlite3.Connection) -> None:
    conn.execute(GATE_TABLE_SCHEMA)
    conn.execute(STREAM_LEASES_SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(schema_migration_gate)").fetchall()}
    if "generation" not in cols:
        try:
            conn.execute("ALTER TABLE schema_migration_gate ADD COLUMN generation INTEGER NOT NULL DEFAULT 1")
        except Exception:
            pass
    if "holder_start_time" not in cols:
        try:
            conn.execute("ALTER TABLE schema_migration_gate ADD COLUMN holder_start_time REAL")
        except Exception:
            pass
    lease_cols = {r[1] for r in conn.execute("PRAGMA table_info(delivery_stream_leases)").fetchall()}
    if "stream_operation_id" not in lease_cols:
        try:
            conn.execute("ALTER TABLE delivery_stream_leases ADD COLUMN stream_operation_id TEXT")
        except Exception:
            pass
    if "http_completed" not in lease_cols:
        try:
            conn.execute("ALTER TABLE delivery_stream_leases ADD COLUMN http_completed INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
    if "delivery_confirmed" not in lease_cols:
        try:
            conn.execute("ALTER TABLE delivery_stream_leases ADD COLUMN delivery_confirmed INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass


def get_file_identity_strict(path: Path | str | None) -> tuple[int, int]:
    """
    Returns unique physical identity (st_dev, st_ino) across hardlinks and symlinks.
    Raises exception (PermissionError, FileNotFoundError, OSError, ValueError) if identity cannot be strictly determined.
    """
    if not path:
        raise ValueError("Empty path provided to get_file_identity_strict")
    p = Path(path).resolve()
    st = p.stat()
    return (st.st_dev, st.st_ino)


def get_file_identity(path: Path | str | None) -> tuple[int, int] | str | None:
    """
    Returns unique physical identity (st_dev, st_ino) across hardlinks and symlinks,
    or case-normalized resolved path.
    """
    if not path:
        return None
    try:
        return get_file_identity_strict(path)
    except Exception:
        try:
            return os.path.normcase(os.path.abspath(str(path)))
        except Exception:
            return str(path)


def get_process_start_time(pid: int) -> float | None:
    """Returns process creation timestamp if available, to protect against PID reuse."""
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            class FILETIME(ctypes.Structure):
                _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]
            c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            if kernel32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                time_val = (c.dwHighDateTime << 32) + c.dwLowDateTime
                return time_val / 10_000_000.0
            return None
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            proc_stat = Path(f"/proc/{pid}/stat")
            if proc_stat.exists():
                parts = proc_stat.read_text().split(")")[-1].split()
                return float(parts[19])
            return os.path.getctime(f"/proc/{pid}")
        except Exception:
            return None


def is_pid_alive(pid: int) -> bool:
    """Cross-platform check whether a process with the given PID is currently alive."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError as err:
            import errno
            return err.errno == errno.EPERM


def is_pid_owner_alive(pid: int, expected_start_time: float | None = None) -> bool:
    """
    Checks if PID is alive AND matches original creation time (fencing against PID reuse).
    """
    if not is_pid_alive(pid):
        return False
    if expected_start_time is not None:
        actual_start_time = get_process_start_time(pid)
        if actual_start_time is not None:
            if abs(actual_start_time - expected_start_time) > 1.0:
                logging.warning(
                    "Detected PID %s recycling: expected start time %s != actual %s",
                    pid, expected_start_time, actual_start_time,
                )
                return False
    return True


def is_migration_active(db_path_or_conn: Path | str | sqlite3.Connection, now: str | None = None) -> bool:
    """
    Returns True if an exclusive cross-process migration gate is currently active, unexpired,
    held by a living process, or if an incomplete/crashed migration has left the database unverified.
    Fails closed (returns True) on any operational or malformed data errors.
    """
    close_after = False
    if isinstance(db_path_or_conn, (str, Path)):
        try:
            conn = sqlite3.connect(db_path_or_conn, timeout=2)
            close_after = True
        except Exception:
            return True
    else:
        conn = db_path_or_conn

    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migration_gate'"
        ).fetchone()
        if not has_table:
            return False
        current_time = now or utc_now()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(schema_migration_gate)").fetchall()}
        has_start_time = "holder_start_time" in cols
        query = (
            "SELECT holder_id, holder_pid, expires_at, holder_start_time FROM schema_migration_gate WHERE gate_name = 'migration'"
            if has_start_time
            else "SELECT holder_id, holder_pid, expires_at, NULL FROM schema_migration_gate WHERE gate_name = 'migration'"
        )
        row = conn.execute(query).fetchone()
        if not row:
            return False
        holder_id, holder_pid, expires_at, holder_start = row[0], row[1], row[2], row[3]
        if not holder_id:
            return False
        # MEDIUM-01: Malformed metadata fails closed
        if not isinstance(expires_at, str):
            return True
        # HIGH-03: Process liveness and identity check with start-time verification
        if holder_pid and is_pid_owner_alive(holder_pid, holder_start):
            return True
        # If holder PID is dead/recycled, check lease expiry:
        try:
            exp_dt = datetime.fromisoformat(expires_at)
            cur_dt = datetime.fromisoformat(current_time)
            if exp_dt > cur_dt:
                return True
        except Exception:
            return True
        return False
    except sqlite3.OperationalError:
        return True
    except Exception:
        # MEDIUM-01: Fail closed on any malformed data or parsing error
        return True
    finally:
        if close_after:
            try:
                conn.close()
            except Exception:
                pass



@retry_busy
def acquire_delivery_stream_lease(
    db_path_or_conn: Path | str | sqlite3.Connection,
    job_id: str,
    lease_seconds: int = DEFAULT_STREAM_LEASE_SECONDS,
    stream_operation_id: str | None = None,
    clock: Any = None,
) -> dict:
    """
    Acquires a transmission lease for an in-flight delivery job.
    Mutual exclusion protocol:
    1. If migration gate is active, raises RuntimeError (fail-closed).
    2. Verifies delivery_jobs record is in state 'processing' and artifact_role 'result'.
    3. Prevents overwriting if held by another living operation.
    4. Records lease in delivery_stream_leases with current PID, process start time,
       timestamps, migration gate generation, and unique stream_operation_id.
    """
    close_after = False
    if isinstance(db_path_or_conn, (str, Path)):
        conn = sqlite3.connect(db_path_or_conn, timeout=BUSY_TIMEOUT)
        close_after = True
    else:
        conn = db_path_or_conn

    try:
        ensure_gate_table(conn)
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if is_migration_active(conn):
            conn.rollback()
            raise RuntimeError("Delivery forbidden: exclusive migration gate is active")

        # Verify job state and role
        row = conn.execute(
            "SELECT artifact_role, state FROM delivery_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not row:
            conn.rollback()
            raise RuntimeError(f"Delivery forbidden: job {job_id} not found")
        if row[0] != "result":
            conn.rollback()
            raise RuntimeError(f"Delivery forbidden: artifact role is '{row[0]}', expected 'result'")
        if row[1] != "processing":
            conn.rollback()
            raise RuntimeError(f"Delivery forbidden: job state is '{row[1]}', expected 'processing'")

        op_id = stream_operation_id or f"sop_{uuid.uuid4().hex}"

        # Fencing against concurrent operation on the same job
        existing = conn.execute(
            "SELECT holder_pid, holder_start_time, stream_operation_id, http_completed, delivery_confirmed "
            "FROM delivery_stream_leases WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if existing:
            e_pid, e_start, e_op, e_http_done, e_deliv_conf = existing[0], existing[1], existing[2], existing[3], existing[4]
            if is_pid_owner_alive(e_pid, e_start) and (e_op is not None and e_op != op_id):
                if not e_deliv_conf:
                    conn.rollback()
                    raise RuntimeError(
                        f"Delivery forbidden: stream lease already held by active operation {e_op} (PID {e_pid})"
                    )

        # Capture generation
        gen_row = conn.execute(
            "SELECT generation FROM schema_migration_gate WHERE gate_name='migration'"
        ).fetchone()
        generation = int(gen_row[0]) if gen_row and gen_row[0] is not None else 1

        pid = os.getpid()
        start_time = get_process_start_time(pid)
        if start_time is None:
            start_time = time.time()
        now_str = clock() if clock is not None else utc_now()
        now_dt = datetime.fromisoformat(now_str)
        expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat()

        conn.execute(
            "INSERT OR REPLACE INTO delivery_stream_leases "
            "(job_id, holder_pid, holder_start_time, acquired_at, expires_at, gate_generation, stream_operation_id, http_completed, delivery_confirmed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)",
            (job_id, pid, start_time, now_str, expires_at, generation, op_id),
        )
        conn.commit()
        return {
            "job_id": job_id,
            "holder_pid": pid,
            "holder_start_time": start_time,
            "acquired_at": now_str,
            "expires_at": expires_at,
            "gate_generation": generation,
            "stream_operation_id": op_id,
            "http_completed": False,
            "delivery_confirmed": False,
        }
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        if close_after:
            try:
                conn.close()
            except Exception:
                pass


@retry_busy
def release_delivery_stream_lease(
    db_path_or_conn: Path | str | sqlite3.Connection,
    job_id: str,
    holder_pid: int | None = None,
    stream_operation_id: str | None = None,
) -> bool:
    """
    Releases the transmission lease for job_id, verifying process and operation identity.
    Uses bounded retries (@retry_busy) on SQLite lock contention.
    """
    close_after = False
    if isinstance(db_path_or_conn, (str, Path)):
        conn = sqlite3.connect(db_path_or_conn, timeout=BUSY_TIMEOUT)
        close_after = True
    else:
        conn = db_path_or_conn

    try:
        ensure_gate_table(conn)
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT holder_pid, holder_start_time, stream_operation_id FROM delivery_stream_leases WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if not row:
            conn.commit()
            return False
        cur_h_pid, cur_h_start, cur_op_id = row[0], row[1], row[2]

        if stream_operation_id is not None and cur_op_id is not None:
            if stream_operation_id != cur_op_id:
                conn.rollback()
                return False

        if holder_pid is not None and cur_h_pid != holder_pid:
            conn.rollback()
            return False

        cursor = conn.execute(
            "DELETE FROM delivery_stream_leases WHERE job_id = ?",
            (job_id,),
        )
        conn.commit()
        return cursor.rowcount > 0
    except sqlite3.OperationalError:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        if close_after:
            try:
                conn.close()
            except Exception:
                pass


@retry_busy
def confirm_delivery_stream_completed(
    db_path_or_conn: Path | str | sqlite3.Connection,
    job_id: str,
    stream_operation_id: str | None = None,
) -> bool:
    """
    Records that HTTP chunk streaming has completed and remote receipt is verified.
    Separates HTTP transmission completion from lease release and state finalization.
    Uses bounded retries (@retry_busy) on SQLite lock contention.
    """
    close_after = False
    if isinstance(db_path_or_conn, (str, Path)):
        conn = sqlite3.connect(db_path_or_conn, timeout=BUSY_TIMEOUT)
        close_after = True
    else:
        conn = db_path_or_conn

    try:
        ensure_gate_table(conn)
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if stream_operation_id:
            cursor = conn.execute(
                "UPDATE delivery_stream_leases SET http_completed = 1, delivery_confirmed = 1 "
                "WHERE job_id = ? AND stream_operation_id = ?",
                (job_id, stream_operation_id),
            )
        else:
            cursor = conn.execute(
                "UPDATE delivery_stream_leases SET http_completed = 1, delivery_confirmed = 1 "
                "WHERE job_id = ?",
                (job_id,),
            )
        conn.commit()
        return cursor.rowcount > 0
    except sqlite3.OperationalError:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        if close_after:
            try:
                conn.close()
            except Exception:
                pass


@retry_busy
def renew_delivery_stream_lease(
    db_path_or_conn: Path | str | sqlite3.Connection,
    job_id: str,
    lease_seconds: int = DEFAULT_STREAM_LEASE_SECONDS,
    stream_operation_id: str | None = None,
    clock: Any = None,
) -> dict:
    """
    Heartbeat renewal protocol for an active streaming upload.
    Extends expires_at to (now + lease_seconds).
    Fails closed (raises RuntimeError) if:
    - Migration gate is active or generation changed
    - Lease is missing or owned by another process
    - Process start time does not match (PID reuse)
    - stream_operation_id does not match (cross-operation collision)
    - Job is no longer 'result' role or not in 'processing' state
    - Database is unavailable or operational error occurs
    """
    close_after = False
    if isinstance(db_path_or_conn, (str, Path)):
        conn = sqlite3.connect(db_path_or_conn, timeout=BUSY_TIMEOUT)
        close_after = True
    else:
        conn = db_path_or_conn

    try:
        ensure_gate_table(conn)
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")

        # 1. Check migration gate
        gate_row = conn.execute(
            "SELECT holder_id, holder_pid, generation, holder_start_time FROM schema_migration_gate WHERE gate_name='migration'"
        ).fetchone()
        if gate_row:
            holder_id, holder_pid, gen, holder_start = gate_row[0], gate_row[1], gate_row[2], gate_row[3]
            if holder_id and holder_pid and is_pid_owner_alive(holder_pid, holder_start):
                conn.rollback()
                raise RuntimeError("Delivery forbidden: migration became active during stream renewal")

        # 2. Check stream lease
        lease_row = conn.execute(
            "SELECT holder_pid, holder_start_time, gate_generation, stream_operation_id FROM delivery_stream_leases WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if not lease_row:
            conn.rollback()
            raise RuntimeError(f"Delivery forbidden: stream lease for job {job_id} not found")

        holder_pid, holder_start, gate_gen, lease_op_id = lease_row[0], lease_row[1], lease_row[2], lease_row[3]
        cur_pid = os.getpid()
        if holder_pid != cur_pid:
            conn.rollback()
            raise RuntimeError(f"Delivery forbidden: stream lease held by PID {holder_pid}, expected {cur_pid}")

        if holder_start is not None:
            cur_start = get_process_start_time(cur_pid)
            if cur_start is not None and abs(cur_start - holder_start) > 1.0:
                conn.rollback()
                raise RuntimeError("Delivery forbidden: PID recycled (start time mismatch)")

        if stream_operation_id is not None and lease_op_id is not None:
            if stream_operation_id != lease_op_id:
                conn.rollback()
                raise RuntimeError(
                    f"Delivery forbidden: stream_operation_id mismatch: {stream_operation_id} != {lease_op_id}"
                )

        # Verify generation against migration gate
        if gate_row and gate_row[2] is not None:
            current_gate_gen = int(gate_row[2])
            if current_gate_gen > int(gate_gen):
                conn.rollback()
                raise RuntimeError("Delivery forbidden: migration gate generation increased")

        # 3. Check delivery_jobs
        has_jobs = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_jobs'"
        ).fetchone()
        if has_jobs:
            job_row = conn.execute(
                "SELECT artifact_role, state FROM delivery_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not job_row:
                conn.rollback()
                raise RuntimeError(f"Delivery forbidden: job {job_id} deleted")
            if job_row[0] != "result":
                conn.rollback()
                raise RuntimeError(f"Delivery forbidden: artifact role changed to '{job_row[0]}'")
            if job_row[1] != "processing":
                conn.rollback()
                raise RuntimeError(f"Delivery forbidden: job state is '{job_row[1]}', expected 'processing'")

        # 4. Renew expires_at
        now_str = clock() if clock is not None else utc_now()
        now_dt = datetime.fromisoformat(now_str)
        new_expires = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        conn.execute(
            "UPDATE delivery_stream_leases SET expires_at = ? WHERE job_id = ? AND holder_pid = ?",
            (new_expires, job_id, cur_pid),
        )
        conn.commit()
        return {
            "job_id": job_id,
            "holder_pid": cur_pid,
            "holder_start_time": holder_start,
            "expires_at": new_expires,
            "gate_generation": gate_gen,
            "stream_operation_id": lease_op_id,
        }
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        if close_after:
            try:
                conn.close()
            except Exception:
                pass


def check_delivery_stream_lease(
    conn: sqlite3.Connection,
    job_id: str,
    expected_generation: int,
    stream_operation_id: str | None = None,
    renew: bool = True,
    lease_seconds: int = DEFAULT_STREAM_LEASE_SECONDS,
    clock: Any = None,
) -> bool:
    """
    Continuous authorization check performed on each chunk during streaming.
    Optionally extends expires_at (heartbeat protocol) on each verified chunk.
    Fails closed (raises RuntimeError) if:
    - Exclusive migration gate has incremented generation or is actively held
    - Delivery stream lease is missing, generation mismatch, or held by another PID / operation
    - Artifact role is no longer 'result' or job state is not 'processing'
    - Any SQLite OperationalError occurs
    """
    try:
        # Check migration gate
        gate_row = conn.execute(
            "SELECT holder_id, holder_pid, generation, holder_start_time FROM schema_migration_gate WHERE gate_name='migration'"
        ).fetchone()
        if gate_row:
            holder_id, holder_pid, gen, holder_start = gate_row[0], gate_row[1], gate_row[2], gate_row[3]
            if holder_id and holder_pid and is_pid_owner_alive(holder_pid, holder_start):
                raise RuntimeError("Delivery forbidden: migration became active during stream")
            if gen is not None and int(gen) > expected_generation:
                raise RuntimeError("Delivery forbidden: migration gate generation increased during stream")

        # Check stream lease
        lease_row = conn.execute(
            "SELECT holder_pid, holder_start_time, expires_at, gate_generation, stream_operation_id FROM delivery_stream_leases WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if not lease_row:
            raise RuntimeError("Delivery forbidden: stream lease was removed")
        cur_pid = os.getpid()
        if lease_row[0] != cur_pid:
            raise RuntimeError("Delivery forbidden: stream lease held by different PID")
        if stream_operation_id is not None and lease_row[4] is not None:
            if stream_operation_id != lease_row[4]:
                raise RuntimeError("Delivery forbidden: stream_operation_id mismatch")
        if int(lease_row[3]) != expected_generation:
            raise RuntimeError("Delivery forbidden: stream lease generation mismatch")

        holder_start = lease_row[1]
        if holder_start is not None:
            cur_start = get_process_start_time(cur_pid)
            if cur_start is not None and abs(cur_start - holder_start) > 1.0:
                raise RuntimeError("Delivery forbidden: PID recycled (start time mismatch)")

        # Check job role and state
        has_jobs = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_jobs'"
        ).fetchone()
        if has_jobs:
            job_row = conn.execute(
                "SELECT artifact_role, state FROM delivery_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not job_row:
                raise RuntimeError("Delivery forbidden: job record deleted")
            if job_row[0] != "result":
                raise RuntimeError(f"Delivery forbidden: artifact role changed to '{job_row[0]}'")
            if job_row[1] != "processing":
                raise RuntimeError(f"Delivery forbidden: job state is '{job_row[1]}', expected 'processing'")

        # Heartbeat: renew expires_at on chunk verification
        if renew:
            now_str = clock() if clock is not None else utc_now()
            now_dt = datetime.fromisoformat(now_str)
            new_expires = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
            retries = 5
            while retries > 0:
                try:
                    conn.execute(
                        "UPDATE delivery_stream_leases SET expires_at = ? WHERE job_id = ? AND holder_pid = ?",
                        (new_expires, job_id, cur_pid),
                    )
                    conn.commit()
                    break
                except sqlite3.OperationalError as op_err:
                    retries -= 1
                    if retries == 0:
                        raise RuntimeError(f"Delivery forbidden: SQLite operational error during lease renewal: {op_err}") from op_err
                    time.sleep(0.02)

        return True
    except sqlite3.OperationalError as exc:
        raise RuntimeError(f"Delivery forbidden: SQLite operational error during stream check: {exc}") from exc


def get_active_stream_leases(
    conn: sqlite3.Connection,
    now: str | None = None,
) -> list[dict]:
    """
    Returns all active stream leases whose holder process is currently alive and active.
    Stale leases from dead PIDs or recycled PIDs are purged automatically.

    CRUCIAL SAFETY INVARIANTS (PHOTODOC-R1-STREAM-LEASE-STATE-FENCING-09):
    1. Expiry of expires_at alone MUST NOT purge the lease or allow migration to proceed
       if the worker process is confirmed alive.
    2. Terminal state of delivery_jobs (delivered/failed) MUST NOT purge the lease if the
       worker process is confirmed alive. As long as the holder process is alive, the lease
       remains ACTIVE to ensure mutual exclusion with migration.
    """
    ensure_gate_table(conn)
    current_time = now or utc_now()
    rows = conn.execute(
        "SELECT job_id, holder_pid, holder_start_time, acquired_at, expires_at, gate_generation, stream_operation_id "
        "FROM delivery_stream_leases"
    ).fetchall()

    active = []
    stale_job_ids = []

    for r in rows:
        jid = r[0]
        pid = r[1]
        start_time = r[2]
        acq_at = r[3]
        exp_at = r[4]
        gen = r[5]
        op_id = r[6] if len(r) > 6 else None

        # 1. Check if holder process is alive and identity matches (fencing against dead/recycled PID)
        is_alive = is_pid_owner_alive(pid, start_time)
        if not is_alive:
            # Process crashed / dead or recycled PID -> stale lease, safe to purge
            stale_job_ids.append(jid)
            continue

        # 2. HOLDER PROCESS IS ALIVE:
        # REQUIREMENT 8: get_active_stream_leases() не должен удалять lease только из-за терминального
        # состояния, если его владелец ещё способен продолжать передачу.
        # As long as the holder process is confirmed ALIVE, the lease remains ACTIVE.
        active.append({
            "job_id": jid,
            "holder_pid": pid,
            "holder_start_time": start_time,
            "acquired_at": acq_at,
            "expires_at": exp_at,
            "gate_generation": gen,
            "stream_operation_id": op_id,
        })

    if stale_job_ids:
        try:
            for jid in stale_job_ids:
                conn.execute("DELETE FROM delivery_stream_leases WHERE job_id=?", (jid,))
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass

    return active


def acquire_migration_gate(
    db_path: Path | str,
    timeout: float = DEFAULT_GATE_TIMEOUT_SECONDS,
    lease_seconds: int = DEFAULT_GATE_LEASE_SECONDS,
    clock: Any = None,
) -> str:
    """
    Exclusively acquires the cross-process migration gate.
    Fails closed (raises RuntimeError) if exclusive access cannot be established within timeout.
    Returns the holder_id token.
    """
    db_path = Path(db_path)
    deadline = time.monotonic() + timeout
    holder_id = uuid.uuid4().hex
    pid = os.getpid()
    start_time = get_process_start_time(pid)
    if start_time is None:
        start_time = time.time()

    # MEDIUM-02: SQLite contention timeout must respect requested timeout
    remaining = max(0.01, deadline - time.monotonic())
    conn = sqlite3.connect(db_path, timeout=remaining)
    try:
        if time.monotonic() >= deadline:
            raise RuntimeError("Cannot acquire exclusive migration gate: timed out waiting for active lock")
        ensure_gate_table(conn)
        conn.commit()

        acquired = False
        while time.monotonic() < deadline:
            now_str = clock() if clock is not None else utc_now()
            now_dt = datetime.fromisoformat(now_str) if clock is not None else datetime.now(timezone.utc)
            expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT holder_id, holder_pid, expires_at, generation, holder_start_time FROM schema_migration_gate WHERE gate_name = 'migration'"
                ).fetchone()
                prev_generation = 0
                if row is not None:
                    existing_holder, existing_pid, existing_expires = row[0], row[1], row[2]
                    existing_gen = row[3] if len(row) > 3 else None
                    existing_start = row[4] if len(row) > 4 else None
                    if existing_gen is not None:
                        try:
                            prev_generation = int(existing_gen)
                        except (TypeError, ValueError):
                            prev_generation = 0
                    if existing_holder and existing_pid:
                        # HIGH-03: If existing holder process is alive, MUST NOT reclaim gate!
                        if is_pid_owner_alive(existing_pid, existing_start):
                            conn.rollback()
                            time.sleep(0.05)
                            continue
                        # Stale lock from dead/crashed process: log and reclaim
                        logging.warning(
                            "Reclaiming migration gate from dead process PID %s (holder %s, expired at %s)",
                            existing_pid, existing_holder, existing_expires,
                        )

                # READER/WRITER EXCLUSION: Migration cannot acquire exclusive gate
                # while active delivery streams are held by living worker processes
                active_streams = get_active_stream_leases(conn, now=now_str)
                if active_streams:
                    conn.rollback()
                    time.sleep(0.05)
                    continue

                next_generation = max(1, prev_generation + 1)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_migration_gate (gate_name, holder_id, holder_pid, acquired_at, expires_at, generation, holder_start_time) "
                    "VALUES ('migration', ?, ?, ?, ?, ?, ?)",
                    (holder_id, pid, now_str, expires_at, next_generation, start_time),
                )
                conn.commit()
                acquired = True
                break
            except sqlite3.OperationalError:
                # Database locked by another write transaction
                time.sleep(0.05)

        if not acquired:
            raise RuntimeError("Cannot acquire exclusive migration gate: timed out waiting for active lock")

        return holder_id
    finally:
        conn.close()


def release_migration_gate(db_path: Path | str, holder_id: str, generation: int | None = None) -> bool:
    """
    Releases the exclusive migration gate if owned by holder_id.
    """
    if not holder_id:
        return False
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("BEGIN IMMEDIATE")
        ensure_gate_table(conn)
        row = conn.execute(
            "SELECT holder_id, generation FROM schema_migration_gate WHERE gate_name = 'migration'"
        ).fetchone()
        if not row or row[0] != holder_id:
            conn.rollback()
            return False
        if generation is not None and len(row) > 1 and row[1] != generation:
            conn.rollback()
            return False
        cursor = conn.execute(
            "UPDATE schema_migration_gate SET holder_id = '', holder_pid = 0, acquired_at = '', "
            "expires_at = '1970-01-01T00:00:00', holder_start_time = NULL "
            "WHERE gate_name = 'migration' AND holder_id = ?",
            (holder_id,),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def enqueue(conn: sqlite3.Connection, *, order_id: int, kind: str,
            local_path: str | None = None, original_name: str | None = None,
            remote_path: str | None = None, sha256: str | None = None,
            size: int | None = None, artifact_role: str | None = None) -> str:
    job_id = uuid.uuid4().hex
    now = utc_now()
    if kind in ("admin_email", "paid_email"):
        cleanup_state = "not_applicable"
        cleanup_phase = "none"
    else:
        cleanup_state = "pending"
        cleanup_phase = "pending"
    # artifact_role is explicitly set by callers (create_order: source, submit_delivery: result)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(delivery_jobs)").fetchall()}
    if "artifact_role" in cols:
        effective_role = artifact_role
        if effective_role is None:
            if kind == "info":
                effective_role = ROLE_INFO
            elif kind == "file":
                effective_role = ROLE_UNKNOWN
        conn.execute(
            "INSERT OR IGNORE INTO delivery_jobs "
            "(id, order_id, kind, artifact_role, local_path, original_name, remote_path, content_sha256, "
            "content_size, next_attempt_at, created_at, cleanup_state, cleanup_phase) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, order_id, kind, effective_role, local_path, original_name, remote_path, sha256, size, now, now, cleanup_state, cleanup_phase),
        )
    else:
        conn.execute(
            "INSERT OR IGNORE INTO delivery_jobs "
            "(id, order_id, kind, local_path, original_name, remote_path, content_sha256, "
            "content_size, next_attempt_at, created_at, cleanup_state, cleanup_phase) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, order_id, kind, local_path, original_name, remote_path, sha256, size, now, now, cleanup_state, cleanup_phase),
        )
    if kind == "paid_email":
        conn.execute("INSERT OR IGNORE INTO paid_email_outbox(order_id,state,updated_at) VALUES(?,'pending',?)", (order_id, now))
    return job_id


def quarantine_expired(conn: sqlite3.Connection, now: str, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> int:
    has_leases = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_stream_leases'"
    ).fetchone()
    if has_leases:
        cols_l = {r[1] for r in conn.execute("PRAGMA table_info(delivery_stream_leases)").fetchall()}
        if "http_completed" in cols_l:
            cursor = conn.execute(
                "UPDATE delivery_jobs SET state = 'failed', lease_until = NULL, "
                "last_error = COALESCE(last_error, 'LeaseExpired') "
                "WHERE state = 'processing' AND lease_until <= ? AND attempts >= ? "
                "AND id NOT IN ("
                "SELECT job_id FROM delivery_stream_leases "
                "WHERE COALESCE(delivery_confirmed, 0) = 1 OR (COALESCE(http_completed, 0) = 0 AND expires_at > ?)"
                ")",
                (now, max_attempts, now),
            )
        else:
            cursor = conn.execute(
                "UPDATE delivery_jobs SET state = 'failed', lease_until = NULL, "
                "last_error = COALESCE(last_error, 'LeaseExpired') "
                "WHERE state = 'processing' AND lease_until <= ? AND attempts >= ? "
                "AND id NOT IN (SELECT job_id FROM delivery_stream_leases WHERE expires_at > ?)",
                (now, max_attempts, now),
            )
    else:
        cursor = conn.execute(
            "UPDATE delivery_jobs SET state = 'failed', lease_until = NULL, "
            "last_error = COALESCE(last_error, 'LeaseExpired') "
            "WHERE state = 'processing' AND lease_until <= ? AND attempts >= ?",
            (now, max_attempts),
        )
    return cursor.rowcount


@retry_busy
def claim_due(db_path: Path, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> dict | None:
    now = utc_now()
    lease = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        if is_migration_active(conn, now):
            conn.commit()
            return None
        # Task 4 / Crash recovery: if gate table still has an unreleased record,
        # migration crashed before clean release. Keep workers blocked until clean migration completes.
        has_gate_tbl = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migration_gate'"
        ).fetchone()
        if has_gate_tbl:
            pending_gate = conn.execute(
                "SELECT 1 FROM schema_migration_gate WHERE gate_name = 'migration' AND holder_id != ''"
            ).fetchone()
            if pending_gate:
                conn.commit()
                return None

        has_stream_leases = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_stream_leases'"
        ).fetchone()
        if has_stream_leases:
            get_active_stream_leases(conn, now)

        quarantine_expired(conn, now, max_attempts)
        conn.execute("""UPDATE paid_email_outbox SET state='uncertain',last_error='WorkerInterrupted',updated_at=?
            WHERE state='sending' AND EXISTS (SELECT 1 FROM delivery_jobs j
                WHERE j.kind='paid_email' AND j.order_id=paid_email_outbox.order_id
                AND (j.lease_until IS NULL OR j.lease_until<=?))""", (now, now))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(delivery_jobs)").fetchall()}
        role_predicate = "(kind != 'file' OR artifact_role = 'result')" if "artifact_role" in cols else "kind != 'file'"
        alias_predicate = ""
        if "artifact_role" in cols:
            alias_predicate = (
                "AND (kind != 'file' OR NOT EXISTS ("
                "SELECT 1 FROM delivery_jobs j2 WHERE j2.id != delivery_jobs.id "
                "AND j2.local_path = delivery_jobs.local_path "
                "AND j2.artifact_role IN ('source', 'quarantine')))"
            )
        stream_lease_predicate = ""
        query_params = [max_attempts, now, max_attempts, now]
        if has_stream_leases:
            cols_l = {r[1] for r in conn.execute("PRAGMA table_info(delivery_stream_leases)").fetchall()}
            if "http_completed" in cols_l:
                stream_lease_predicate = (
                    "AND id NOT IN ("
                    "SELECT job_id FROM delivery_stream_leases "
                    "WHERE COALESCE(http_completed, 0) = 0 AND expires_at > ?"
                    ") "
                )
            else:
                stream_lease_predicate = (
                    "AND id NOT IN ("
                    "SELECT job_id FROM delivery_stream_leases "
                    "WHERE expires_at > ?"
                    ") "
                )
            query_params.append(now)
        row = conn.execute(
            "SELECT * FROM delivery_jobs WHERE "
            "COALESCE(remote_path,'') NOT LIKE 'remote/%' AND "
            f"{role_predicate} "
            f"{alias_predicate} AND "
            "((state IN ('pending', 'failed') AND attempts < ? AND next_attempt_at <= ?) OR "
            "(state = 'processing' AND attempts < ? AND lease_until <= ?)) "
            "AND (kind NOT IN ('file','info','paid_email') OR EXISTS "
            "(SELECT 1 FROM orders o WHERE o.id=delivery_jobs.order_id AND o.payment_status='paid')) "
            "AND (kind!='paid_email' OR NOT EXISTS (SELECT 1 FROM paid_email_outbox e "
            "WHERE e.order_id=delivery_jobs.order_id AND e.state IN ('sending','uncertain'))) "
            f"{stream_lease_predicate}"
            "ORDER BY created_at, id LIMIT 1", tuple(query_params),
        ).fetchone()
        if row is None:
            conn.commit()
            return None

        # Check alias conflicts and physical identity for candidate file job
        if row["kind"] == "file":
            cand_path = row["local_path"]
            if not cand_path:
                conn.commit()
                return None
            try:
                cand_fid = get_file_identity_strict(cand_path)
                cand_norm = os.path.normcase(os.path.abspath(cand_path))
            except Exception:
                # Inability to establish identity of candidate must fail closed (block claiming)
                conn.commit()
                return None

            for p_row in conn.execute(
                "SELECT local_path FROM delivery_jobs WHERE id != ? AND artifact_role IN ('source', 'quarantine')",
                (row["id"],),
            ).fetchall():
                p_path = p_row[0]
                if not p_path:
                    continue
                p_norm = os.path.normcase(os.path.abspath(p_path))
                if p_norm == cand_norm:
                    conn.commit()
                    return None
                try:
                    p_fid = get_file_identity_strict(p_path)
                    if p_fid == cand_fid:
                        conn.commit()
                        return None
                except Exception:
                    # Inability to establish identity of a protected record MUST fail closed!
                    # Do not treat inability to establish identity as absence of a conflicting alias.
                    conn.commit()
                    return None

        conn.execute(
            "UPDATE delivery_jobs SET state='processing', attempts=attempts+1, lease_until=? WHERE id=?",
            (lease, row["id"]),
        )
        conn.commit()
        claimed = dict(row)
        claimed["lease_until"] = lease
        claimed["attempts"] = row["attempts"] + 1
        claimed["state"] = "processing"
        return claimed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@retry_busy
def finish(
    db_path: Path,
    job_id: str,
    error: Exception | None = None,
    lease_until: str | None = None,
    stream_operation_id: str | None = None,
) -> bool:
    """
    Finalizes delivery attempt in SQLite.
    Returns True if delivery succeeded and was committed under the active lease.
    Returns False if delivery failed or if the lease expired / was stolen by another worker.
    """
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT)
    committed = False
    try:
        conn.execute("BEGIN IMMEDIATE")

        # Fencing against concurrent state mutation during active streaming
        has_leases = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_stream_leases'"
        ).fetchone()
        if has_leases:
            cols_l = {r[1] for r in conn.execute("PRAGMA table_info(delivery_stream_leases)").fetchall()}
            has_http_col = "http_completed" in cols_l
            http_q = ", http_completed, delivery_confirmed" if has_http_col else ", 1, 1"
            lease_row = conn.execute(
                f"SELECT holder_pid, holder_start_time, stream_operation_id{http_q} FROM delivery_stream_leases WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if lease_row:
                holder_pid, holder_start, lease_op_id = lease_row[0], lease_row[1], lease_row[2]
                http_done = bool(lease_row[3])
                deliv_confirmed = bool(lease_row[4])
                if is_pid_owner_alive(holder_pid, holder_start):
                    # Active transmission is in flight by a live worker process!
                    # Requirement 7: Never allow finish() to release or finalize based on PID alone!
                    if stream_operation_id is None or (lease_op_id and stream_operation_id != lease_op_id):
                        conn.rollback()
                        return False
                    # Requirement 8: finish() with valid token CANNOT prematurely finalize an active HTTP stream!
                    if error is None:
                        if not http_done or not deliv_confirmed:
                            conn.rollback()
                            return False
                    # Authorized finalizing: release the stream lease atomically
                    conn.execute("DELETE FROM delivery_stream_leases WHERE job_id=?", (job_id,))
                else:
                    # Worker process is confirmed dead; clean up stale lease
                    conn.execute("DELETE FROM delivery_stream_leases WHERE job_id=?", (job_id,))

        if error is None:
            if is_migration_active(conn):
                conn.execute(
                    "UPDATE delivery_jobs SET state='failed', lease_until=NULL, last_error='MigrationGateActive' "
                    "WHERE id=? AND state='processing'", (job_id,)
                )
                conn.commit()
                return False

            cols = {r[1] for r in conn.execute("PRAGMA table_info(delivery_jobs)").fetchall()}
            has_role = "artifact_role" in cols
            query = "SELECT kind, order_id, artifact_role FROM delivery_jobs WHERE id=?" if has_role else "SELECT kind, order_id FROM delivery_jobs WHERE id=?"
            row = conn.execute(query, (job_id,)).fetchone()
            if not row:
                return False
            kind, order_id = row[0], row[1]
            if kind in ("file", "info", "paid_email"):
                paid = conn.execute("SELECT payment_status FROM orders WHERE id=?", (order_id,)).fetchone()
                if not paid or paid[0] != "paid":
                    return False
            if kind == "file":
                if not has_role or (len(row) > 2 and row[2] != "result"):
                    # Only verified result files can be marked delivered
                    conn.execute(
                        "UPDATE delivery_jobs SET state='failed', lease_until=NULL, last_error='ForbiddenNonResultRole' "
                        "WHERE id=? AND state='processing'", (job_id,)
                    )
                    conn.commit()
                    return False
            if lease_until is not None:
                cursor = conn.execute(
                    "UPDATE delivery_jobs SET state='delivered', lease_until=NULL, delivered_at=?, last_error=NULL "
                    "WHERE id=? AND state='processing' AND lease_until=?", (utc_now(), job_id, lease_until),
                )
            else:
                cursor = conn.execute(
                    "UPDATE delivery_jobs SET state='delivered', lease_until=NULL, delivered_at=?, last_error=NULL "
                    "WHERE id=? AND state='processing'", (utc_now(), job_id),
                )
            committed = cursor.rowcount > 0
        else:
            row = conn.execute("SELECT attempts FROM delivery_jobs WHERE id=?", (job_id,)).fetchone()
            if row is not None:
                attempts = row[0]
                delay = min(3600, 5 * (2 ** min(max(0, attempts - 1), 9)))
                next_time = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                if lease_until is not None:
                    conn.execute(
                        "UPDATE delivery_jobs SET state='failed', lease_until=NULL, next_attempt_at=?, last_error=? "
                        "WHERE id=? AND state='processing' AND lease_until=?",
                        (next_time, type(error).__name__, job_id, lease_until),
                    )
                else:
                    conn.execute(
                        "UPDATE delivery_jobs SET state='failed', lease_until=NULL, next_attempt_at=?, last_error=? "
                        "WHERE id=? AND state='processing'",
                        (next_time, type(error).__name__, job_id),
                    )
            committed = False
        conn.commit()
        return committed
    finally:
        conn.close()


def _is_cleanup_lease_valid(conn: sqlite3.Connection, job_id: str, token: str) -> bool:
    """
    Verifies that this worker still holds the active lease token before any filesystem mutation.
    Returns False if the lease expired or was acquired by another worker.
    """
    row = conn.execute(
        "SELECT cleanup_lease_until FROM delivery_jobs "
        "WHERE id=? AND cleanup_state='processing'",
        (job_id,),
    ).fetchone()
    if not row or row[0] != token:
        return False
    # Verify expiration timestamp part of token has not passed
    lease_ts = token.split("#")[0]
    return lease_ts > utc_now()


def _mark_cleanup_completed(db_path: Path, job_id: str, token: str | None = None) -> bool:
    """
    Atomically records cleanup completion if the worker still holds the lease.
    """
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        now_ts = utc_now()
        if token is not None:
            cursor = conn.execute(
                "UPDATE delivery_jobs SET cleanup_state='completed', cleanup_phase='deleted', local_deleted_at=?, "
                "cleanup_lease_until=NULL, cleanup_error=NULL "
                "WHERE id=? AND cleanup_state='processing' AND cleanup_lease_until=?",
                (now_ts, job_id, token),
            )
        else:
            cursor = conn.execute(
                "UPDATE delivery_jobs SET cleanup_state='completed', cleanup_phase='deleted', local_deleted_at=?, "
                "cleanup_lease_until=NULL, cleanup_error=NULL WHERE id=?",
                (now_ts, job_id),
            )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def _mark_cleanup_failed(db_path: Path, job_id: str, token: str | None = None, error: str = "") -> bool:
    """
    Atomically records cleanup failure with bounded exponential backoff if the worker still holds the lease.
    """
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        row = conn.execute("SELECT cleanup_attempts FROM delivery_jobs WHERE id=?", (job_id,)).fetchone()
        attempts = row[0] if row else 1
        delay = min(3600, 5 * (2 ** min(max(0, attempts - 2), 8))) if attempts > 1 else 0
        backoff_until = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        if token is not None:
            cursor = conn.execute(
                "UPDATE delivery_jobs SET cleanup_state='failed', cleanup_phase='failed', cleanup_lease_until=?, "
                "cleanup_error=? WHERE id=? AND cleanup_state='processing' AND cleanup_lease_until=?",
                (backoff_until, error, job_id, token),
            )
        else:
            cursor = conn.execute(
                "UPDATE delivery_jobs SET cleanup_state='failed', cleanup_phase='failed', cleanup_lease_until=?, "
                "cleanup_error=? WHERE id=?",
                (backoff_until, error, job_id),
            )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def perform_job_cleanup(
    db_path: Path,
    job_id: str,
    uploads_dir: Path | None = None,
    pre_unlink_hook: Any = None,
    clock: Any = None,
    lease_duration_seconds: int = 120,
) -> bool:
    """
    Safely and idempotently removes a delivered photo or info.txt file from local disk.

    Strict Safety Guards:
    1. Preconditions: state == 'delivered' and kind in ('file', 'info').
    2. Lease & Concurrency: atomically acquires a unique fencing lease token to prevent worker collisions.
    3. Confinement: path MUST be strictly inside uploads_dir without '..' or symlinks.
    4. Isolation: local file MUST match the order_id prefix to prevent cross-order file manipulation.
    5. Identity check: checks size and sha256 against delivery_jobs metadata before unlinking.
    6. Dependency check: checks that NO other unfinished delivery job references this local_path.
    7. Durable Intent: commits cleanup_phase='deleting' before unlinking to enable crash recovery.
    8. Pre-unlink fencing: verifies active lease ownership in write transaction before unlinking.
    9. Idempotency & Crash recovery: if file is already deleted and cleanup_phase=='deleting',
       records completed cleanly only if conditional finalization succeeds.
       If deleted without intent, flags as MissingSourceBeforeCleanup anomaly.
    10. Failure resilience: cleanup error never marks delivery state back to pending; applies bounded backoff.
    """
    if uploads_dir is None:
        uploads_dir = get_uploads_dir()
    uploads_resolved = uploads_dir.resolve()

    now = clock() if clock is not None else utc_now()
    now_dt = datetime.fromisoformat(now) if clock is not None else datetime.now(timezone.utc)
    lease_ts = (now_dt + timedelta(seconds=lease_duration_seconds)).isoformat()
    nonce = uuid.uuid4().hex[:8]

    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    token = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        if is_migration_active(conn, now):
            conn.commit()
            return False
        cols = {r[1] for r in conn.execute("PRAGMA table_info(delivery_jobs)").fetchall()}
        has_role = "artifact_role" in cols
        job = conn.execute(
            "SELECT id, order_id, kind, local_path, content_sha256, content_size, state, "
            "cleanup_state, cleanup_phase, cleanup_attempts, cleanup_lease_until, cleanup_error"
            + (", artifact_role" if has_role else "") +
            " FROM delivery_jobs WHERE id=?",
            (job_id,),
        ).fetchone()

        if not job:
            conn.commit()
            return False

        # Invariant C: only verified result files may be deleted by delivery cleanup.
        # Original customer photos, quarantined files, and unverified/null roles must NEVER be deleted.
        if job["kind"] == "file":
            if not has_role or job["artifact_role"] != "result":
                raw_path = job["local_path"]
                file_present = False
                if raw_path:
                    p = Path(raw_path)
                    if p.is_file():
                        file_present = True
                    elif uploads_dir is not None and (uploads_dir / p.name).is_file():
                        file_present = True
                if file_present:
                    # File physically exists: NEVER delete non-result files!
                    conn.commit()
                    return False

        # Only delivered jobs can have their local files cleaned up
        if job["state"] != "delivered":
            conn.commit()
            return False

        paid = conn.execute("SELECT payment_status FROM orders WHERE id=?", (job["order_id"],)).fetchone()
        if job["kind"] in ("file", "info") and (not paid or paid[0] != "paid"):
            conn.commit()
            return False

        # Email jobs don't have local uploads to clean
        if job["kind"] not in ("file", "info"):
            conn.execute(
                "UPDATE delivery_jobs SET cleanup_state='not_applicable', cleanup_phase='none' WHERE id=?", (job_id,)
            )
            conn.commit()
            return False

        # Already completed cleanup
        if job["cleanup_state"] == "completed":
            conn.commit()
            return True

        # Non-retryable anomalies: never auto-retry unexplained missing sources
        if job["cleanup_error"] in ("HistoricalMissingSource", "MissingSourceBeforeCleanup", "MissingLocalPath"):
            conn.commit()
            return False

        # Check if another active worker holds a valid cleanup lease
        if job["cleanup_state"] == "processing" and job["cleanup_lease_until"]:
            active_ts = job["cleanup_lease_until"].split("#")[0]
            if active_ts > now:
                conn.commit()
                return False

        # Check if failed job is still in backoff or exceeded max attempts
        if job["cleanup_state"] == "failed":
            if job["cleanup_attempts"] >= 10:
                conn.commit()
                return False
            if job["cleanup_lease_until"]:
                backoff_ts = job["cleanup_lease_until"].split("#")[0]
                if backoff_ts > now:
                    conn.commit()
                    return False

        next_attempt = job["cleanup_attempts"] + 1
        token = f"{lease_ts}#{next_attempt}#{nonce}"

        # Atomically claim cleanup processing with fencing token
        cursor = conn.execute(
            "UPDATE delivery_jobs SET cleanup_state='processing', cleanup_lease_until=?, "
            "cleanup_attempts=? WHERE id=?",
            (token, next_attempt, job_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return False
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    raw_local_path = job["local_path"]
    if not raw_local_path:
        _mark_cleanup_failed(db_path, job_id, token, "MissingLocalPath")
        return False

    path_obj = Path(raw_local_path)

    # Path traversal check
    if ".." in raw_local_path or any(part == ".." for part in path_obj.parts):
        _mark_cleanup_failed(db_path, job_id, token, "PathTraversalDetected")
        return False

    # Symlink protection on input path
    if path_obj.is_symlink():
        _mark_cleanup_failed(db_path, job_id, token, "SymlinkDetected")
        return False

    try:
        target_resolved = path_obj.resolve()
    except Exception as exc:
        _mark_cleanup_failed(db_path, job_id, token, f"PathResolveError: {exc}")
        return False

    # Uploads confinement check
    if uploads_resolved not in target_resolved.parents and target_resolved.parent != uploads_resolved:
        _mark_cleanup_failed(db_path, job_id, token, "PathOutsideUploads")
        return False

    # Symlink protection on resolved target
    if target_resolved.is_symlink():
        _mark_cleanup_failed(db_path, job_id, token, "SymlinkDetected")
        return False

    # Isolation check: file must belong to this order
    if not path_obj.name.startswith(f"{job['order_id']}_"):
        _mark_cleanup_failed(db_path, job_id, token, "OrderIdMismatchInFilename")
        return False

    # Check if file is already absent:
    # Ambiguity note:
    # When cleanup_phase == 'deleting' and path_obj does not exist, the application
    # has durable proof that deletion intent was committed by a valid lease-holding worker.
    # We conclude crash recovery took place around the unlink syscall.
    # However, if an external process or storage fault removed the file between intent
    # commitment and recovery, this cannot be distinguished at the SQLite layer from
    # the application's own unlinking. The contract guarantees durable worker intent,
    # not cryptographic proof of OS unlink provenance.
    if not path_obj.exists():
        if job["cleanup_phase"] == "deleting":
            # Durable crash recovery: app started deletion and crashed around unlink/completion
            return _mark_cleanup_completed(db_path, job_id, token)
        else:
            # Check if a sibling job with the same local_path already legally deleted the file with durable evidence
            conn = sqlite3.connect(db_path, timeout=10)
            sibling_deleted = 0
            try:
                sibling_deleted = conn.execute(
                    "SELECT COUNT(*) FROM delivery_jobs "
                    "WHERE local_path=? AND id!=? AND cleanup_state='completed' "
                    "AND cleanup_phase='deleted' AND local_deleted_at IS NOT NULL",
                    (raw_local_path, job_id),
                ).fetchone()[0]
            finally:
                conn.close()

            if sibling_deleted > 0:
                # Sibling job already performed the physical deletion with durable evidence!
                return _mark_cleanup_completed(db_path, job_id, token)

            # File missing before application deletion intent was ever established
            _mark_cleanup_failed(db_path, job_id, token, "MissingSourceBeforeCleanup")
            return False

    # Must be a regular file
    if not path_obj.is_file():
        _mark_cleanup_failed(db_path, job_id, token, "NotARegularFile")
        return False

    # Historical/incomplete jobs must not authorize deletion without an identity
    # that can be compared with the verified remote delivery.
    expected_size = job["content_size"]
    expected_sha256 = job["content_sha256"]
    if (not isinstance(expected_size, int) or expected_size < 0
            or not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256) is None):
        _mark_cleanup_failed(db_path, job_id, token, "MissingDeliveryIdentity")
        return False

    # Pre-delete identity verification: size
    try:
        actual_size = path_obj.stat().st_size
    except Exception as exc:
        _mark_cleanup_failed(db_path, job_id, token, f"StatError: {exc}")
        return False

    if expected_size is not None and actual_size != expected_size:
        _mark_cleanup_failed(
            db_path, job_id, token, f"SizeMismatchBeforeDelete: actual={actual_size} expected={expected_size}"
        )
        return False

    # Pre-delete identity verification: SHA-256
    if expected_sha256:
        try:
            actual_sha256 = sha256_file(path_obj).lower()
        except Exception as exc:
            _mark_cleanup_failed(db_path, job_id, token, f"ChecksumError: {exc}")
            return False

        if actual_sha256 != expected_sha256.lower():
            _mark_cleanup_failed(
                db_path, job_id, token, f"Sha256MismatchBeforeDelete: actual={actual_sha256} expected={expected_sha256}"
            )
            return False

    # Dependency check: check if any OTHER unfinished job still references this file,
    # OR if any job classifies this file as source or quarantine (protected customer artifact)
    try:
        target_fid = get_file_identity_strict(raw_local_path)
        target_norm = os.path.normcase(os.path.abspath(raw_local_path))
    except Exception as exc:
        _mark_cleanup_failed(db_path, job_id, token, f"TargetIdentityError: {exc}")
        return False

    conn = sqlite3.connect(db_path, timeout=10)
    try:
        cols_c = {r[1] for r in conn.execute("PRAGMA table_info(delivery_jobs)").fetchall()}
        has_role_col = "artifact_role" in cols_c
        role_q = ", artifact_role" if has_role_col else ", 'result'"
        rows = conn.execute(
            f"SELECT id, local_path{role_q}, state FROM delivery_jobs WHERE id != ?",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()

    has_protected_alias = False
    has_unfinished_ref = False
    for other_id, other_lpath, other_role, other_state in rows:
        if not other_lpath:
            continue
        other_norm = os.path.normcase(os.path.abspath(other_lpath))
        is_same = (other_norm == target_norm)
        if not is_same:
            try:
                other_fid = get_file_identity_strict(other_lpath)
                if other_fid == target_fid:
                    is_same = True
            except Exception:
                if other_role in ("source", "quarantine"):
                    # Fail closed: unable to verify identity of a protected record -> treat as protected!
                    has_protected_alias = True
                    break
        if is_same:
            if other_role in ("source", "quarantine"):
                has_protected_alias = True
                break
            if other_state != "delivered":
                has_unfinished_ref = True

    if has_protected_alias:
        # File is associated with a protected source/quarantine artifact; NEVER delete!
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            conn.execute(
                "UPDATE delivery_jobs SET cleanup_state='not_applicable', cleanup_phase='none', cleanup_lease_until=NULL, "
                "cleanup_error='ProtectedArtifactLinkedToSourceOrQuarantine' WHERE id=? AND cleanup_lease_until=?",
                (job_id, token),
            )
            conn.commit()
        finally:
            conn.close()
        return False

    if has_unfinished_ref:
        # File is still needed by another active task; postpone cleanup
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            conn.execute(
                "UPDATE delivery_jobs SET cleanup_state='pending', cleanup_phase='pending', cleanup_lease_until=NULL, "
                "cleanup_error='FileStillReferencedByUnfinishedJob' WHERE id=? AND cleanup_lease_until=?",
                (job_id, token),
            )
            conn.commit()
        finally:
            conn.close()
        return False

    # DURABLE INTENT PHASE:
    # Commit cleanup_phase='deleting' to the database before filesystem mutation.
    # Verifies lease ownership is still valid before recording intent.
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE delivery_jobs SET cleanup_phase='deleting' "
            "WHERE id=? AND cleanup_state='processing' AND cleanup_lease_until=?",
            (job_id, token),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            return False
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # Pre-unlink hook for deterministic testing
    if pre_unlink_hook is not None:
        pre_unlink_hook(job_id, token)

    # SYNCHRONIZED DELETION & FENCING CRITICAL SECTION:
    # A short SQLite write transaction that serializes lease verification,
    # filesystem mutation, and state finalization.
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if is_migration_active(conn):
            conn.rollback()
            return False
        cols_check = {r[1] for r in conn.execute("PRAGMA table_info(delivery_jobs)").fetchall()}
        has_role_check = "artifact_role" in cols_check
        role_select = ", artifact_role" if has_role_check else ""
        row = conn.execute(
            f"SELECT cleanup_lease_until, cleanup_state, cleanup_attempts, state, local_path, "
            f"content_sha256, content_size, kind, order_id{role_select} FROM delivery_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return False

        current_token, current_state, current_attempts = row[0], row[1], row[2]
        if current_token != token or current_state != "processing":
            # Lease was stolen or altered by another worker; stale worker aborts without unlinking!
            conn.rollback()
            return False

        lease_ts_check = token.split("#")[0]
        now_check = clock() if clock is not None else utc_now()
        if lease_ts_check <= now_check:
            # Lease expired; abort without unlinking
            conn.rollback()
            return False

        # Fail-closed check: non-result files must NEVER be deleted
        if row[7] == "file":
            if not has_role_check or (len(row) > 9 and row[9] != "result"):
                conn.rollback()
                return False

        # Earlier checks and the durable intent are not a deletion authorization:
        # another worker may have changed delivery or added a consumer since then.
        # BEGIN IMMEDIATE fences these checks against enqueue/state transitions.
        if tuple(row[3:9]) != (
            "delivered", raw_local_path, expected_sha256, expected_size, job["kind"], job["order_id"]
        ):
            conn.rollback()
            _mark_cleanup_failed(db_path, job_id, token, "DeliveryChangedBeforeDelete")
            return False
        paid = conn.execute("SELECT payment_status FROM orders WHERE id=?", (job["order_id"],)).fetchone()
        if not paid or paid[0] != "paid":
            conn.rollback()
            return False
        if conn.execute(
            "SELECT 1 FROM delivery_jobs WHERE local_path=? AND id!=? AND state!='delivered' LIMIT 1",
            (raw_local_path, job_id),
        ).fetchone():
            conn.execute(
                "UPDATE delivery_jobs SET cleanup_state='pending', cleanup_phase='pending', "
                "cleanup_lease_until=NULL, cleanup_error='FileStillReferencedByUnfinishedJob' "
                "WHERE id=? AND cleanup_lease_until=?", (job_id, token),
            )
            conn.commit()
            return False
        if has_role_check:
            has_protected_conflict = False
            for other_lpath, other_role in conn.execute(
                "SELECT local_path, artifact_role FROM delivery_jobs WHERE id != ? AND artifact_role IN ('source', 'quarantine')",
                (job_id,),
            ).fetchall():
                if not other_lpath:
                    continue
                if os.path.normcase(os.path.abspath(other_lpath)) == target_norm:
                    has_protected_conflict = True
                    break
                try:
                    if get_file_identity_strict(other_lpath) == target_fid:
                        has_protected_conflict = True
                        break
                except Exception:
                    has_protected_conflict = True
                    break
            if has_protected_conflict:
                conn.rollback()
                return False

        # Recheck the actual bytes at the filesystem boundary, including a
        # same-size substitution. Application uploads use immutable UUID paths.
        # An absent source after durable deleting intent follows crash recovery.
        try:
            source_changed = path_obj.is_symlink() or path_obj.resolve() != target_resolved
            if path_obj.exists():
                source_changed = (source_changed or not path_obj.is_file()
                                  or path_obj.stat().st_size != expected_size
                                  or sha256_file(path_obj).lower() != expected_sha256.lower())
        except OSError:
            source_changed = True
        if source_changed:
            conn.rollback()
            _mark_cleanup_failed(db_path, job_id, token, "SourceChangedBeforeDelete")
            return False

        # Worker still owns valid lease. Unlink local file:
        try:
            path_obj.unlink()
        except FileNotFoundError:
            pass  # Already unlinked
        except Exception as exc:
            delay = min(3600, 5 * (2 ** min(max(0, current_attempts - 2), 8))) if current_attempts > 1 else 0
            backoff_until = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            conn.execute(
                "UPDATE delivery_jobs SET cleanup_state='failed', cleanup_phase='failed', cleanup_lease_until=?, "
                "cleanup_error=? WHERE id=? AND cleanup_lease_until=?",
                (backoff_until, f"UnlinkError: {type(exc).__name__}: {exc}", job_id, token),
            )
            conn.commit()
            return False

        now_completed = clock() if clock is not None else utc_now()
        cursor = conn.execute(
            "UPDATE delivery_jobs SET cleanup_state='completed', cleanup_phase='deleted', local_deleted_at=?, "
            "cleanup_lease_until=NULL, cleanup_error=NULL WHERE id=? AND cleanup_lease_until=?",
            (now_completed, job_id, token),
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def process_due_cleanups(
    db_path: Path,
    uploads_dir: Path | None = None,
    limit: int = 20,
    clock: Any = None,
) -> int:
    """
    Sweeps delivery_jobs for delivered tasks that still need local file cleanup.
    Recovers from crashes and retries failed cleanups with bounded backoff.
    """
    now = clock() if clock is not None else utc_now()
    conn = sqlite3.connect(db_path, timeout=10)
    candidate_ids = []
    try:
        if is_migration_active(conn, now):
            return 0
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_jobs'"
        ).fetchone()
        if not has_table:
            return 0
        cols = {row[1] for row in conn.execute("PRAGMA table_info(delivery_jobs)").fetchall()}
        if "cleanup_state" not in cols:
            return 0
        role_predicate = "AND (kind != 'file' OR artifact_role = 'result')" if "artifact_role" in cols else "AND kind != 'file'"
        rows = conn.execute(
            "SELECT id FROM delivery_jobs "
            "WHERE state='delivered' AND kind IN ('file', 'info') "
            f"{role_predicate} "
            # Eligibility, not just permission: an unpaid order's source must be
            # retained, so it must never occupy a slot in the sweep window.
            # Without this predicate a backlog of older unpaid jobs keeps
            # re-entering every sweep and starves newer paid cleanups, and each
            # futile attempt still takes a write lock in perform_job_cleanup.
            "AND EXISTS (SELECT 1 FROM orders o "
            "            WHERE o.id=delivery_jobs.order_id AND o.payment_status='paid') "
            "AND ("
            "  cleanup_state = 'pending' "
            "  OR (cleanup_state = 'failed' AND cleanup_attempts < 10 "
            "      AND (cleanup_lease_until IS NULL OR cleanup_lease_until <= ?) "
            "      AND (cleanup_error IS NULL OR cleanup_error NOT IN ('MissingSourceBeforeCleanup', 'HistoricalMissingSource', 'MissingLocalPath'))"
            "  ) "
            "  OR (cleanup_state = 'processing' AND cleanup_lease_until <= ?)"
            ") "
            "ORDER BY created_at, id LIMIT ?",
            (now, now, limit),
        ).fetchall()
        candidate_ids = [r[0] for r in rows]
    finally:
        conn.close()

    cleaned = 0
    for jid in candidate_ids:
        if perform_job_cleanup(db_path, jid, uploads_dir=uploads_dir, clock=clock):
            cleaned += 1
    return cleaned


def cleanup_orphaned_temporary_files(
    uploads_dir: Path,
    db_path: Path,
    max_age_seconds: int = 3600,
) -> int:
    """
    Safely cleans up orphaned temporary upload files (.incoming-*, .tmp*, etc.)
    older than max_age_seconds that are not referenced by any active delivery job.
    Never deletes order files by age alone.
    """
    if not uploads_dir.is_dir():
        return 0

    now = time.time()
    removed = 0

    referenced_paths = set()
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_jobs'"
        ).fetchone()
        if has_table:
            rows = conn.execute(
                "SELECT local_path FROM delivery_jobs WHERE local_path IS NOT NULL"
            ).fetchall()
            for r in rows:
                if r[0]:
                    try:
                        referenced_paths.add(Path(r[0]).resolve())
                    except Exception:
                        pass
    finally:
        conn.close()

    uploads_resolved = uploads_dir.resolve()
    for entry in uploads_dir.iterdir():
        # Match only recognized temporary prefixes/suffixes
        is_temp = (
            entry.name.startswith(".incoming-")
            or entry.name.startswith(".tmp")
            or entry.name.endswith(".tmp")
        )
        if not is_temp:
            # Strictly do not delete order files or unrecognized files by age!
            continue

        try:
            if entry.is_symlink():
                continue
            resolved = entry.resolve()
            if uploads_resolved not in resolved.parents and resolved.parent != uploads_resolved:
                continue
            if not entry.is_file():
                continue

            stat = entry.stat()
            age = now - stat.st_mtime
            if age < max_age_seconds:
                # Within protective threshold (in-flight upload)
                continue

            if resolved in referenced_paths:
                continue

            entry.unlink()
            removed += 1
        except Exception as exc:
            logging.warning("Failed to remove orphaned temp file %s: %s", entry.name, exc)

    return removed


def process_due(db_path: Path, deliver, limit: int = 20, uploads_dir: Path | None = None) -> int:
    processed = 0
    for _ in range(limit):
        if is_migration_active(db_path):
            break
        job = claim_due(db_path)
        if job is None:
            break
        lease = job.get("lease_until")
        stream_op_id = None
        try:
            deliver_res = deliver(job)
            if isinstance(deliver_res, dict):
                stream_op_id = deliver_res.get("stream_operation_id")
            elif isinstance(deliver_res, str):
                stream_op_id = deliver_res
            if not stream_op_id:
                stream_op_id = job.get("stream_operation_id")
        except Exception as exc:
            stream_op_id = job.get("stream_operation_id")
            logging.warning("Delivery job %s failed: %s", job["id"], type(exc).__name__)
            finish(db_path, job["id"], exc, lease_until=lease, stream_operation_id=stream_op_id)
        else:
            committed = finish(db_path, job["id"], lease_until=lease, stream_operation_id=stream_op_id)
            if committed and job.get("kind") in ("file", "info"):
                try:
                    perform_job_cleanup(db_path, job["id"], uploads_dir=uploads_dir)
                except Exception:
                    logging.exception("Immediate cleanup failed for job %s", job["id"])
        processed += 1

    try:
        process_due_cleanups(db_path, uploads_dir=uploads_dir, limit=limit)
    except Exception:
        logging.exception("Background cleanup sweep failed")

    return processed


def worker_loop(
    db_path: Path,
    deliver,
    stop: threading.Event,
    interval: float = 2.0,
    uploads_dir: Path | None = None,
) -> None:
    last_temp_cleanup = 0.0
    while not stop.is_set():
        try:
            process_due(db_path, deliver, uploads_dir=uploads_dir)
            now = time.time()
            if now - last_temp_cleanup > 300:
                last_temp_cleanup = now
                udir = uploads_dir or get_uploads_dir()
                cleanup_orphaned_temporary_files(udir, db_path)
        except Exception:
            logging.exception("Delivery worker iteration failed")
        stop.wait(interval)
