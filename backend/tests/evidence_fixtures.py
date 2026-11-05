"""Fixtures for the Stage 1 evidence tests (not a test module).

Builds synthetic databases containing the shapes that produced false PASSes:
`WITHOUT ROWID` composite keys, shadowed rowid aliases, AUTOINCREMENT high-water
marks, typed values that are equal under `==` but different in SQLite, and a
schema with indexes, triggers, views, CHECK constraints and foreign keys.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
SCRIPTS_DIR = REPO_DIR / "scripts"
for _p in (str(BACKEND_DIR), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import backup_format  # noqa: E402
import db_evidence  # noqa: E402
import recovery_artifacts  # noqa: E402

APPLICATION_ID = 1346650441
NOW = datetime.now(timezone.utc)


def build_database(path: Path, *, variant: str = "base") -> Path:
    """Create the canonical test database, optionally with one deliberate change."""
    path = Path(path)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        conn.execute("PRAGMA user_version = 7")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer TEXT NOT NULL,
                amount TEXT CHECK (length(amount) > 0),
                note TEXT DEFAULT 'none',
                order_token_hash TEXT
            );
            CREATE TABLE delivery_jobs (
                id TEXT PRIMARY KEY,
                order_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                local_path TEXT,
                content_sha256 TEXT,
                content_size INTEGER,
                state TEXT NOT NULL DEFAULT 'pending',
                cleanup_state TEXT NOT NULL DEFAULT 'pending',
                cleanup_phase TEXT NOT NULL DEFAULT 'none',
                local_deleted_at TEXT,
                delivered_at TEXT
            );
            CREATE TABLE wr_items (
                order_id INTEGER NOT NULL,
                line_no  INTEGER NOT NULL,
                payload  TEXT,
                PRIMARY KEY (order_id, line_no)
            ) WITHOUT ROWID;
            CREATE TABLE shadowed (
                rowid TEXT,
                value TEXT
            );
            CREATE TABLE typed (
                id INTEGER PRIMARY KEY,
                payload
            );
            CREATE TABLE constrained (id INTEGER PRIMARY KEY, v TEXT CHECK (length(v) >= 1));
            CREATE TABLE parents (id INTEGER PRIMARY KEY);
            CREATE TABLE children (
                id INTEGER PRIMARY KEY,
                parent_id INTEGER REFERENCES parents(id)
            );
            CREATE INDEX idx_orders_customer ON orders(customer);
            CREATE TRIGGER trg_orders_ai AFTER INSERT ON orders
            BEGIN UPDATE orders SET note = 'seen' WHERE id = NEW.id; END;
            CREATE VIEW v_orders AS SELECT id, customer FROM orders;
            """
        )
        conn.executemany("INSERT INTO orders (customer, amount, note) VALUES (?,?,?)",
                         [("ann", "10.00", "a"), ("bob", "20.00", "b")])
        conn.executemany("INSERT INTO wr_items VALUES (?,?,?)",
                         [(1, 1, "first"), (1, 2, "second"), (2, 1, "third")])
        conn.execute("INSERT INTO shadowed VALUES ('x', 'one')")
        conn.execute("INSERT INTO shadowed VALUES ('y', 'two')")
        conn.executemany("INSERT INTO typed (id, payload) VALUES (?,?)",
                         [(1, "1"), (2, 1), (3, 1.0), (4, -0.0), (5, b"\x00"), (6, None)])
        conn.execute("INSERT INTO constrained VALUES (1, 'abc')")
        conn.execute("INSERT INTO parents VALUES (1)")
        conn.execute("INSERT INTO children VALUES (1, 1)")
        conn.commit()

        if variant == "wr_row":
            conn.execute("UPDATE wr_items SET payload='CHANGED' WHERE order_id=1 AND line_no=2")
        elif variant == "wr_composite_collision":
            # same first key column, different second: the classic collapse
            conn.execute("DELETE FROM wr_items WHERE order_id=1 AND line_no=2")
            conn.execute("INSERT INTO wr_items VALUES (1, 2, 'other')")
        elif variant == "shadow_row":
            conn.execute("UPDATE shadowed SET value='CHANGED' WHERE rowid='y'")
        elif variant == "backup_only_row":
            conn.execute("INSERT INTO orders (customer, amount, note) VALUES ('eve','30.00','c')")
        elif variant == "sequence":
            conn.execute("INSERT INTO orders (customer, amount, note) VALUES ('zed','40.00','d')")
            conn.execute("DELETE FROM orders WHERE customer='zed'")
        elif variant == "typed_text_vs_int":
            conn.execute("UPDATE typed SET payload='1' WHERE id=2")
        elif variant == "typed_negative_zero":
            conn.execute("UPDATE typed SET payload=0.0 WHERE id=4")
        elif variant == "extra_index":
            conn.execute("CREATE INDEX idx_orders_amount ON orders(amount)")
        elif variant == "changed_trigger":
            conn.execute("DROP TRIGGER trg_orders_ai")
        elif variant == "extra_table":
            conn.execute("CREATE TABLE sneaky (id INTEGER PRIMARY KEY, v TEXT)")
        elif variant == "fk_violation":
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("INSERT INTO children VALUES (2, 99)")
        elif variant == "unkeyable":
            conn.execute("CREATE TABLE unkeyable (rowid TEXT, _rowid_ TEXT, oid TEXT)")
            conn.execute("INSERT INTO unkeyable VALUES ('a','b','c')")
        elif variant == "virtual":
            conn.execute("CREATE VIRTUAL TABLE vtab USING fts5(body)")
        conn.commit()
    finally:
        conn.close()
    return path


def copy_database(source: Path, target: Path) -> Path:
    """Consistent copy through the SQLite backup API, not a file copy."""
    target = Path(target)
    if target.exists():
        target.unlink()
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def make_uploads(path: Path, files: Optional[Dict[str, bytes]] = None) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    for name, data in (files or {}).items():
        (path / name).write_bytes(data)
    return path


def uploads_tar(uploads_dir: Path, target: Path,
                extra_members: Optional[list] = None) -> Path:
    with tarfile.open(target, "w:gz") as tar:
        info = tarfile.TarInfo("uploads")
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        tar.addfile(info)
        for entry in sorted(Path(uploads_dir).iterdir()):
            if entry.is_file():
                tar.add(str(entry), arcname=f"uploads/{entry.name}")
        for member, data in (extra_members or []):
            tar.addfile(member, io.BytesIO(data) if data is not None else None)
    return target


def make_set(live_db: Path, destination: Path, *,
             created_at: Optional[datetime] = None,
             verification: str = "PASS",
             upload_files: Optional[Dict[str, bytes]] = None,
             mutate_manifest=None,
             corrupt: Optional[str] = None) -> Path:
    """Produce a complete, checksum-valid Stage 1 backup set.

    With ``upload_files=None`` the archive is derived from the database, so a fixture
    that requires files must say so explicitly rather than accidentally shipping an
    archive the contract would reject.
    """
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    backup_db = copy_database(live_db, destination / backup_format.DB_FILE)

    if upload_files is None:
        required, groups, _errors, _stats = recovery_artifacts.required_sources(backup_db)
        needed = sorted(set(required) | {g["candidates"][0] for g in groups})
        if needed:
            raise ValueError(
                "this fixture requires recovery files; pass upload_files= explicitly: "
                + ", ".join(needed))
        upload_files = {}

    uploads = make_uploads(destination.parent / f"uploads-{destination.name}", upload_files)
    archive = uploads_tar(uploads, destination / backup_format.TAR_FILE)

    schema = db_evidence.canonical_schema(db_evidence.open_readonly(backup_db))
    backup_conn = db_evidence.open_readonly(backup_db)
    try:
        logical = db_evidence.logical_state_digest(backup_conn)
    finally:
        backup_conn.close()
    info, _ = backup_format.inspect_archive(archive)

    artifacts = {
        backup_format.DB_FILE: backup_format.sha256_file(backup_db),
        backup_format.TAR_FILE: backup_format.sha256_file(archive),
    }
    manifest = backup_format.build_manifest(
        artifacts=artifacts,
        created_at=created_at or NOW,
        application_id=APPLICATION_ID,
        schema_digest=schema["digest"],
        logical_digest=logical,
        database_identity={"application_id": APPLICATION_ID, "schema_digest": schema["digest"]},
        upload_members=info["members"],
        upload_bytes=info["bytes"],
        verification=verification,
    )
    if mutate_manifest is not None:
        mutate_manifest(manifest)
    # Exercise the production writer: it writes the manifest and then the checksum
    # file that covers all three artifacts, so the fixture cannot drift from it.
    backup_format.write_manifest_files(destination, manifest)

    if corrupt == "manifest_edit":
        _edit_manifest(destination, {"verification": "SKIPPED"})
    elif corrupt == "hash_disagreement":
        _edit_manifest(destination, {"schema_digest": "f" * 64}, rebind=True)
    elif corrupt == "remove_checksum":
        path = destination / backup_format.MANIFEST_FILE
        kept = [line for line in path.read_text(encoding="utf-8").splitlines()
                if backup_format.MANIFEST_JSON not in line]
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    elif corrupt == "extra_file":
        (destination / "stowaway.txt").write_text("x", encoding="utf-8")
    elif corrupt == "bad_format":
        _edit_manifest(destination, {"format_version": 1}, rebind=True)
    elif corrupt == "drop_digest":
        _edit_manifest(destination, {"logical_digest": None}, rebind=True)
    return destination


def _edit_manifest(destination: Path, updates: Dict[str, Any], *, rebind: bool = False) -> None:
    path = destination / backup_format.MANIFEST_JSON
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.update(updates)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    if rebind:
        lines = [f"{backup_format.sha256_file(destination / name)}  {name}"
                 for name in backup_format.CHECKSUMMED]
        (destination / backup_format.MANIFEST_FILE).write_text(
            "\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------- delivery sets
GOOD_PHOTO = b"correct recovery original"
OTHER_PHOTO = b"second recovery original"


def build_delivery_database(path: Path, uploads_dir: Path, *,
                            photo: bytes = GOOD_PHOTO,
                            second: bytes = OTHER_PHOTO,
                            cleaned: bool = True) -> Path:
    """A database whose delivery jobs require verifiable recovery originals.

    One pending ``file`` job, one ``info`` job, and (optionally) a delivered job with
    durable deletion evidence whose file must *not* be required.
    """
    import hashlib

    path = Path(path)
    uploads_dir = Path(uploads_dir)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    (uploads_dir / "photo.jpg").write_bytes(photo)
    (uploads_dir / "info.pdf").write_bytes(second)

    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        conn.executescript(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_status TEXT,
                order_token_hash TEXT
            );
            CREATE TABLE delivery_jobs (
                id TEXT PRIMARY KEY,
                order_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                state TEXT NOT NULL,
                local_path TEXT,
                content_sha256 TEXT,
                content_size INTEGER,
                cleanup_state TEXT DEFAULT 'pending',
                cleanup_phase TEXT,
local_deleted_at TEXT,
                delivered_at TEXT
            );
            """
        )
        conn.execute("INSERT INTO orders (id, payment_status) VALUES (1,'paid')")
        conn.execute("INSERT INTO orders (id, payment_status) VALUES (2,'paid')")
        conn.execute(
            "INSERT INTO delivery_jobs (id, order_id, kind, state, local_path, "
            "content_sha256, content_size) VALUES (?,?,?,?,?,?,?)",
            ("job-file", 1, "file", "pending", "/app/uploads/photo.jpg",
             hashlib.sha256(photo).hexdigest(), len(photo)))
        conn.execute(
            "INSERT INTO delivery_jobs (id, order_id, kind, state, local_path, "
            "content_sha256, content_size) VALUES (?,?,?,?,?,?,?)",
            ("job-info", 2, "info", "pending", "/app/uploads/info.pdf",
             hashlib.sha256(second).hexdigest(), len(second)))
        if cleaned:
            conn.execute(
                "INSERT INTO delivery_jobs (id, order_id, kind, state, local_path, "
                "content_sha256, content_size, cleanup_state, cleanup_phase, "
                "local_deleted_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("job-cleaned", 1, "file", "delivered", "/app/uploads/gone.jpg",
                 hashlib.sha256(b"deleted long ago").hexdigest(), 18,
                 "completed", "deleted", "2026-01-01T00:10:00+00:00"))
        conn.commit()
    finally:
        conn.close()
    return path


def _logical_digest(conn) -> str:
    """Same shape as db_evidence's logical digest, over one database."""
    import hashlib
    import json as _json

    inventory = db_evidence.table_inventory(conn)
    parts = []
    for table in sorted(inventory):
        if inventory[table].type == "view":
            continue
        columns = db_evidence.table_columns(conn, table)
        identity = db_evidence.row_identity(table, inventory[table], columns)
        rows = db_evidence.count_rows(conn, table)
        distinct = db_evidence.count_distinct_keys(conn, table, identity)
        parts.append(_json.dumps({"table": table, "identity": identity.description,
                                  "rows": rows, "compared": distinct},
                                 sort_keys=True, separators=(",", ":")))
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()
