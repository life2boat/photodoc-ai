"""B1 — the one-time pre-outbox backup gate transition.

The staging database predates PR #26 and has no ``paid_email_outbox`` table, so the
standard gate would refuse the very deployment that creates it. The transition flag
relaxes exactly that requirement, and only while both sides genuinely lack the table.

Because the gate now also proves the backup preserves the live rows, every fixture
here takes a **real backup of the live database** rather than building two
independent databases that merely look alike.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
SCRIPTS_DIR = REPO_DIR / "scripts"
for _p in (str(BACKEND_DIR), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import backup_gate  # noqa: E402
from backup_set_builder import (  # noqa: E402
    age_orders,
    backup_from_live,
    build_database,
)

NOW = datetime.now(timezone.utc)
_PAIRS: dict[str, tuple[Path, Path]] = {}


# --------------------------------------------------------------------------- fixtures
def _pair(tmp_path: Path) -> tuple[Path, Path]:
    """(backup, live) where the backup is a genuine snapshot of the live database."""
    key = str(tmp_path)
    if key not in _PAIRS:
        live = tmp_path / "live" / "orders.db"
        uploads = tmp_path / "live" / "uploads"
        build_database(live, uploads, with_outbox=False)
        age_orders(live, NOW - timedelta(hours=1))
        # The fixture models a quiet database: no job is mid-flight, so the gate's
        # quiescence requirement can be satisfied on both sides of the comparison.
        conn = sqlite3.connect(live)
        try:
            conn.execute("UPDATE delivery_jobs SET state='pending', lease_until=NULL "
                         "WHERE state='processing'")
            conn.commit()
        finally:
            conn.close()
        backup, _ = backup_from_live(live, uploads, tmp_path / "backups" / "20260101T000000Z")
        _PAIRS[key] = (backup, live)
    return _PAIRS[key]


def legacy_set(tmp_path: Path, *, created: datetime | None = None,
               name: str = "20260101T000000Z") -> Path:
    backup, _ = _pair(tmp_path)
    if created is not None:
        payload = json.loads((backup / "backup_manifest.json").read_text(encoding="utf-8"))
        payload["timestamp"] = created.isoformat()
        (backup / "backup_manifest.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8")
    return backup


def legacy_live(tmp_path: Path, *, with_outbox: bool = False) -> Path:
    _, live = _pair(tmp_path)
    if with_outbox:
        from paid_email_outbox import EMAIL_SCHEMA
        conn = sqlite3.connect(live)
        try:
            conn.executescript(EMAIL_SCHEMA)
            conn.commit()
        finally:
            conn.close()
    return live


def transition_args(backup_dir: Path, live_db: Path, *extra) -> list:
    return ["--backup-dir", str(backup_dir), "--allow-pre-outbox-transition",
            "--live-db", str(live_db), "--max-age-hours", "48", *extra]


def refresh_manifests(backup_dir: Path) -> None:
    """Recompute the checksums after deliberately mutating the backup database."""
    db = backup_dir / "orders.db"
    db_sha = hashlib.sha256(db.read_bytes()).hexdigest()
    tar_sha = hashlib.sha256((backup_dir / "uploads.tar.gz").read_bytes()).hexdigest()
    (backup_dir / "manifest.sha256").write_text(
        f"{db_sha}  orders.db\n{tar_sha}  uploads.tar.gz\n", encoding="utf-8")
    payload = json.loads((backup_dir / "backup_manifest.json").read_text(encoding="utf-8"))
    payload["orders_db_sha256"] = db_sha
    payload["uploads_tar_sha256"] = tar_sha
    (backup_dir / "backup_manifest.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")


def run_gate(argv):
    return backup_gate.main(argv)


# ------------------------------------------------------------------- happy path (1)
def test_valid_legacy_database_and_backup_is_allowed(tmp_path, capsys):
    backup_dir = legacy_set(tmp_path)
    live = legacy_live(tmp_path)

    code = run_gate(transition_args(backup_dir, live, "--deep", "--json"))
    payload = json.loads(capsys.readouterr().out)

    assert code == 0, payload.get("errors")
    assert payload["ok"] is True
    assert payload["transition"] is True
    transition = payload["checks"]["pre_outbox_transition"]
    assert transition["live_has_outbox"] is False
    assert transition["backup_has_outbox"] is False
    assert transition["applicable"] is True
    assert "PRE-OUTBOX TRANSITION" in payload["warning"]
    assert payload["checks"]["backup_set"]["sqlite_integrity"] == "ok"
    assert payload["checks"]["backup_set"].get("upload_archive_verified") is True
    # evidence was compared row by row, not counted
    assert payload["checks"]["evidence"]["orders_compared"] >= 5


def test_transition_emits_a_prominent_non_json_warning(tmp_path, capsys):
    backup_dir = legacy_set(tmp_path)
    live = legacy_live(tmp_path)
    assert run_gate(transition_args(backup_dir, live)) == 0
    out = capsys.readouterr().out
    assert "####" in out
    assert "PRE-OUTBOX TRANSITION" in out


# ------------------------------------------------------- standard gate still strict
def test_flag_absent_and_outbox_required_still_refuses(tmp_path, capsys):
    backup_dir = legacy_set(tmp_path)
    code = run_gate(["--backup-dir", str(backup_dir), "--require-outbox", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == backup_gate.EXIT_UNVERIFIED
    assert any("outbox" in e for e in payload["errors"])


def test_flag_absent_without_require_outbox_passes_the_plain_check(tmp_path):
    backup_dir = legacy_set(tmp_path)
    assert run_gate(["--backup-dir", str(backup_dir)]) == 0


def test_require_outbox_and_transition_are_mutually_exclusive(tmp_path, capsys):
    backup_dir = legacy_set(tmp_path)
    live = legacy_live(tmp_path)
    code = run_gate(["--backup-dir", str(backup_dir), "--require-outbox",
                     "--allow-pre-outbox-transition", "--live-db", str(live), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == backup_gate.EXIT_CONFIG
    assert any("mutually exclusive" in e for e in payload["errors"])


def test_transition_requires_live_db(tmp_path, capsys):
    backup_dir = legacy_set(tmp_path)
    code = run_gate(["--backup-dir", str(backup_dir), "--allow-pre-outbox-transition", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == backup_gate.EXIT_CONFIG
    assert any("requires --live-db" in e for e in payload["errors"])


# ------------------------------------------------------------- refusals (2,3,4,5,6,7,8)
def test_stale_legacy_backup_is_refused(tmp_path, capsys):
    backup_dir = legacy_set(tmp_path, created=NOW - timedelta(hours=72))
    live = legacy_live(tmp_path)
    code = run_gate(["--backup-dir", str(backup_dir), "--allow-pre-outbox-transition",
                     "--live-db", str(live), "--max-age-hours", "24", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code != 0
    assert any("too old" in e for e in payload["errors"])


def test_missing_upload_is_refused_even_with_the_flag(tmp_path, capsys):
    backup_dir = legacy_set(tmp_path)
    tar_path = backup_dir / "uploads.tar.gz"
    with tarfile.open(tar_path, "r:gz") as tar:
        members = [(m, tar.extractfile(m).read() if m.isreg() else None)
                   for m in tar.getmembers()]
    victim = [m for m, _ in members if m.isreg() and m.name.endswith(".jpg")][0]
    with tarfile.open(tar_path, "w:gz") as tar:
        for member, data in members:
            if member.name == victim.name:
                continue
            tar.addfile(member, io.BytesIO(data) if data is not None else None)
    refresh_manifests(backup_dir)

    live = legacy_live(tmp_path)
    code = run_gate(transition_args(backup_dir, live, "--deep", "--json"))
    payload = json.loads(capsys.readouterr().out)
    assert code != 0
    assert payload["checks"]["backup_set"].get("upload_archive_verified") is False


def test_corrupted_legacy_backup_is_refused(tmp_path, capsys):
    backup_dir = legacy_set(tmp_path)
    with (backup_dir / "orders.db").open("r+b") as handle:
        handle.seek(0)
        handle.write(b"CORRUPTED-NOT-A-DATABASE")
    live = legacy_live(tmp_path)
    code = run_gate(transition_args(backup_dir, live, "--json"))
    payload = json.loads(capsys.readouterr().out)
    assert code != 0
    assert payload["ok"] is False


def test_live_database_already_has_outbox_is_refused(tmp_path, capsys):
    """The transition is over: the flag must stop working, not quietly downgrade."""
    backup_dir = legacy_set(tmp_path)
    live = legacy_live(tmp_path, with_outbox=True)
    code = run_gate(transition_args(backup_dir, live, "--json"))
    payload = json.loads(capsys.readouterr().out)
    assert code == backup_gate.EXIT_UNVERIFIED
    assert any("already contains paid_email_outbox" in e for e in payload["errors"])
    assert payload["checks"]["pre_outbox_transition"]["live_has_outbox"] is True


def test_backup_already_has_outbox_is_refused(tmp_path, capsys):
    from paid_email_outbox import EMAIL_SCHEMA

    backup_dir = legacy_set(tmp_path)
    conn = sqlite3.connect(backup_dir / "orders.db")
    try:
        conn.executescript(EMAIL_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    refresh_manifests(backup_dir)

    live = legacy_live(tmp_path)
    code = run_gate(transition_args(backup_dir, live, "--json"))
    payload = json.loads(capsys.readouterr().out)
    assert code == backup_gate.EXIT_UNVERIFIED
    assert any("backup already contains paid_email_outbox" in e for e in payload["errors"])


def test_identity_mismatch_is_refused(tmp_path, capsys):
    backup_dir = legacy_set(tmp_path)
    db = backup_dir / "orders.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA application_id=0")
        conn.commit()
    finally:
        conn.close()
    refresh_manifests(backup_dir)

    live = legacy_live(tmp_path)
    code = run_gate(transition_args(backup_dir, live, "--json"))
    payload = json.loads(capsys.readouterr().out)
    assert code != 0
    assert any("application_id" in e for e in payload["errors"])


def test_email_evidence_loss_is_refused(tmp_path, capsys):
    """A backup holding fewer paid_email jobs than live could lose the attempt record."""
    backup_dir = legacy_set(tmp_path)
    conn = sqlite3.connect(backup_dir / "orders.db")
    try:
        victim = conn.execute(
            "SELECT order_id FROM delivery_jobs WHERE kind='paid_email' ORDER BY order_id LIMIT 1"
        ).fetchone()
        assert victim is not None
        conn.execute("DELETE FROM delivery_jobs WHERE kind='paid_email' AND order_id=?",
                     (victim[0],))
        conn.commit()
    finally:
        conn.close()
    refresh_manifests(backup_dir)

    live = legacy_live(tmp_path)
    code = run_gate(transition_args(backup_dir, live, "--json"))
    payload = json.loads(capsys.readouterr().out)
    assert code == backup_gate.EXIT_UNVERIFIED
    assert any("would lose the record" in e or "email" in e for e in payload["errors"])


def test_an_order_missing_from_the_backup_is_refused(tmp_path, capsys):
    backup_dir = legacy_set(tmp_path)
    live = legacy_live(tmp_path)
    conn = sqlite3.connect(live)
    try:
        conn.execute(
            "INSERT INTO orders (id, name, filename, payment_status, created_at) "
            "VALUES (999, 'Later order', 'later.jpg', 'unpaid', ?)",
            ((NOW - timedelta(minutes=1)).isoformat(),))
        conn.commit()
    finally:
        conn.close()

    code = run_gate(transition_args(backup_dir, live, "--json"))
    payload = json.loads(capsys.readouterr().out)
    assert code != 0
    assert any("does not contain order #999" in e or "predates" in e
               for e in payload["errors"])


# --------------------------------------------------------------- shell integration
def _bash() -> str | None:
    for candidate in (r"C:\Program Files\Git\usr\bin\bash.exe",
                      r"C:\Program Files\Git\bin\bash.exe", "/bin/bash", "/usr/bin/bash"):
        if Path(candidate).is_file():
            return candidate
    return shutil.which("bash")


BASH = _bash()
GATE_SCRIPT = SCRIPTS_DIR / "deploy_backup_gate.sh"


def run_wrapper(backup_root: Path, live_db: Path, **env_overrides):
    env = dict(os.environ)
    env.update({
        "REPO_ROOT": REPO_DIR.as_posix(),
        "PYTHON_BIN": sys.executable,
        "GATE_MODE": "host",
        "BACKUP_ROOT": backup_root.as_posix(),
        "LIVE_DB": live_db.as_posix(),
        "BACKUP_MAX_AGE_HOURS": "48",
    })
    for key in ("BACKUP_GATE_ALLOW_PRE_OUTBOX", "SKIP_BACKUP_GATE", "DB_ABSENCE_PROVEN"):
        env.pop(key, None)
    env.update(env_overrides)
    if BASH and "Git" in BASH:
        git_root = Path(BASH).parent.parent
        env["PATH"] = os.pathsep.join(
            [str(git_root / "usr" / "bin"), str(git_root / "bin"), env.get("PATH", "")])
    return subprocess.run([BASH, GATE_SCRIPT.as_posix()], env=env,
                          capture_output=True, text=True, timeout=240)


@pytest.mark.skipif(BASH is None, reason="bash unavailable")
def test_wrapper_refuses_legacy_backup_without_the_flag(tmp_path):
    backup_dir = legacy_set(tmp_path)
    live = legacy_live(tmp_path)
    result = run_wrapper(backup_dir.parent, live)
    assert result.returncode == 1, result.stderr
    assert "refused this deployment" in result.stderr


@pytest.mark.skipif(BASH is None, reason="bash unavailable")
def test_wrapper_allows_legacy_backup_with_the_flag(tmp_path):
    backup_dir = legacy_set(tmp_path)
    live = legacy_live(tmp_path)
    result = run_wrapper(backup_dir.parent, live, BACKUP_GATE_ALLOW_PRE_OUTBOX="1")
    assert result.returncode == 0, result.stderr
    assert "BACKUP_GATE_ALLOW_PRE_OUTBOX=1" in result.stderr
    assert "WARNING" in result.stderr


@pytest.mark.skipif(BASH is None, reason="bash unavailable")
def test_wrapper_flag_stops_working_after_the_transition(tmp_path):
    backup_dir = legacy_set(tmp_path)
    live = legacy_live(tmp_path, with_outbox=True)
    result = run_wrapper(backup_dir.parent, live, BACKUP_GATE_ALLOW_PRE_OUTBOX="1")
    assert result.returncode == 1, result.stderr
    assert "refused this deployment" in result.stderr


@pytest.mark.skipif(BASH is None, reason="bash unavailable")
def test_wrapper_never_treats_the_flag_as_a_skip(tmp_path):
    """The transition is independent of SKIP_BACKUP_GATE and must not bypass it."""
    backup_dir = legacy_set(tmp_path)
    live = legacy_live(tmp_path)
    result = run_wrapper(backup_dir.parent, live, BACKUP_GATE_ALLOW_PRE_OUTBOX="1",
                         SKIP_BACKUP_GATE="1")
    assert result.returncode == 2, result.stderr
    assert "SKIP_BACKUP_GATE=1" in result.stderr


@pytest.mark.skipif(BASH is None, reason="bash unavailable")
def test_wrapper_passes_the_flag_in_both_modes():
    text = GATE_SCRIPT.read_text(encoding="utf-8")
    assert text.count('"${GATE_FLAGS[@]}"') == 2, "both invocation paths must forward the flags"
    assert "--allow-pre-outbox-transition" in text
    assert 'GATE_FLAGS+=(--require-outbox)' in text
    assert "--deep" in text
