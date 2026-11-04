"""B1 — the one-time pre-outbox backup gate transition.

The staging database predates PR #26 and has no ``paid_email_outbox`` table. The
standard gate requires that table, so it refused the very deployment that creates
it. ``--allow-pre-outbox-transition`` / ``BACKUP_GATE_ALLOW_PRE_OUTBOX=1`` relaxes
only that requirement, and only while both databases genuinely lack the table.

Every test here is about the *narrowness* of that relaxation: it must never let a
stale, corrupted, incomplete or identity-mismatched backup through, it must refuse
once the transition is over, and it must not be a back door around the outbox
requirement.
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
from backup_set_builder import build_database, make_backup_set  # noqa: E402

NOW = datetime.now(timezone.utc)


# --------------------------------------------------------------------------- helpers
def run_gate(argv):
    return backup_gate.main(argv)


def legacy_set(tmp_path: Path, *, created=None, name="20260101T000000Z"):
    """A verified backup produced from a pre-PR #26 database (no outbox table)."""
    backup_dir, _ = make_backup_set(tmp_path / "backups", name=name,
                                    with_outbox=False, created=created or NOW)
    return backup_dir


def legacy_live(tmp_path: Path, *, with_outbox: bool = False) -> Path:
    """A live database, with order timestamps older than the backup.

    ``build_database`` stamps one order with ``CURRENT_TIMESTAMP``, i.e. a few
    milliseconds *after* a backup created at module import. That would trip the
    coverage check in every test and hide the behaviour under test, so the
    timestamps are normalised into the recent past.
    """
    db = tmp_path / "live" / "orders.db"
    build_database(db, tmp_path / "live" / "uploads", with_outbox=with_outbox)
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE orders SET created_at=?",
                     ((NOW - timedelta(hours=1)).isoformat(),))
        conn.commit()
    finally:
        conn.close()
    return db


def transition_args(backup_dir: Path, live_db: Path, *extra) -> list:
    return ["--backup-dir", str(backup_dir), "--allow-pre-outbox-transition",
            "--live-db", str(live_db), "--max-age-hours", "48", *extra]


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
    # the standard checks still ran
    assert payload["checks"]["backup_set"]["sqlite_integrity"] == "ok"
    assert payload["checks"]["backup_set"]["application_id"] is not None
    assert "paid_email_outbox" not in (payload["checks"]["backup_set"]["tables"] or [])
    assert payload["checks"]["backup_set"].get("upload_archive_verified") is True


def test_transition_emits_a_prominent_non_json_warning(tmp_path, capsys):
    backup_dir = legacy_set(tmp_path)
    live = legacy_live(tmp_path)
    assert run_gate(transition_args(backup_dir, live)) == 0
    out = capsys.readouterr().out
    assert "####" in out
    assert "PRE-OUTBOX TRANSITION" in out
    assert "Pre-outbox transition:    requested" in out


# ------------------------------------------------------- standard gate still strict
def test_flag_absent_and_outbox_required_still_refuses(tmp_path, capsys):
    backup_dir = legacy_set(tmp_path)
    code = run_gate(["--backup-dir", str(backup_dir), "--require-outbox", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == backup_gate.EXIT_UNVERIFIED
    assert any("outbox" in e for e in payload["errors"])


def test_flag_absent_without_require_outbox_passes_the_plain_check(tmp_path):
    """Nothing changes for callers that never asked for the outbox requirement."""
    backup_dir = legacy_set(tmp_path)
    assert run_gate(["--backup-dir", str(backup_dir)]) == 0


def test_require_outbox_and_transition_are_mutually_exclusive(tmp_path, capsys):
    backup_dir = legacy_set(tmp_path)
    live = legacy_live(tmp_path)
    code = run_gate(["--backup-dir", str(backup_dir), "--require-outbox",
                     "--allow-pre-outbox-transition", "--live-db", str(live), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == backup_gate.EXIT_UNVERIFIED
    assert any("mutually exclusive" in e for e in payload["errors"])


def test_transition_requires_live_db(tmp_path, capsys):
    backup_dir = legacy_set(tmp_path)
    code = run_gate(["--backup-dir", str(backup_dir), "--allow-pre-outbox-transition", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == backup_gate.EXIT_UNVERIFIED
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
    # drop a required upload and keep the manifest self-consistent
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
    tar_sha = hashlib.sha256(tar_path.read_bytes()).hexdigest()
    db_sha = hashlib.sha256((backup_dir / "orders.db").read_bytes()).hexdigest()
    (backup_dir / "manifest.sha256").write_text(
        f"{db_sha}  orders.db\n{tar_sha}  uploads.tar.gz\n", encoding="utf-8")
    manifest = json.loads((backup_dir / "backup_manifest.json").read_text(encoding="utf-8"))
    manifest["uploads_tar_sha256"] = tar_sha
    (backup_dir / "backup_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")

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
    backup_dir, _ = make_backup_set(tmp_path / "backups", with_outbox=True, created=NOW)
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
    db_sha = hashlib.sha256(db.read_bytes()).hexdigest()
    tar_sha = hashlib.sha256((backup_dir / "uploads.tar.gz").read_bytes()).hexdigest()
    (backup_dir / "manifest.sha256").write_text(
        f"{db_sha}  orders.db\n{tar_sha}  uploads.tar.gz\n", encoding="utf-8")
    manifest = json.loads((backup_dir / "backup_manifest.json").read_text(encoding="utf-8"))
    manifest["orders_db_sha256"] = db_sha
    (backup_dir / "backup_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")

    live = legacy_live(tmp_path)
    code = run_gate(transition_args(backup_dir, live, "--json"))
    payload = json.loads(capsys.readouterr().out)
    assert code != 0
    assert any("application_id" in e for e in payload["errors"])


def test_email_evidence_loss_is_refused(tmp_path, capsys):
    """A backup holding fewer paid_email jobs than the live database could lose evidence."""
    backup_dir = legacy_set(tmp_path)
    live = legacy_live(tmp_path)

    # Remove one paid_email job from the BACKUP so it holds less evidence than live.
    db = backup_dir / "orders.db"
    conn = sqlite3.connect(db)
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
    db_sha = hashlib.sha256(db.read_bytes()).hexdigest()
    tar_sha = hashlib.sha256((backup_dir / "uploads.tar.gz").read_bytes()).hexdigest()
    (backup_dir / "manifest.sha256").write_text(
        f"{db_sha}  orders.db\n{tar_sha}  uploads.tar.gz\n", encoding="utf-8")
    manifest = json.loads((backup_dir / "backup_manifest.json").read_text(encoding="utf-8"))
    manifest["orders_db_sha256"] = db_sha
    (backup_dir / "backup_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")

    code = run_gate(transition_args(backup_dir, live, "--json"))
    payload = json.loads(capsys.readouterr().out)
    assert code == backup_gate.EXIT_UNVERIFIED
    evidence = payload["checks"]["pre_outbox_transition"]["evidence"]
    assert evidence["paid_email_jobs"]["backup"] < evidence["paid_email_jobs"]["live"]
    assert any("email-delivery evidence could be lost" in e for e in payload["errors"])


def test_backup_newer_than_orders_but_lacking_a_new_order_is_refused(tmp_path, capsys):
    """Coverage still applies: the backup must contain every live order."""
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
    assert any("fewer orders" in e or "predates" in e for e in payload["errors"])


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
    env.pop("BACKUP_GATE_ALLOW_PRE_OUTBOX", None)
    env.pop("SKIP_BACKUP_GATE", None)
    env.update(env_overrides)
    if BASH and "Git" in BASH:
        git_root = Path(BASH).parent.parent
        env["PATH"] = os.pathsep.join(
            [str(git_root / "usr" / "bin"), str(git_root / "bin"), env.get("PATH", "")])
    return subprocess.run([BASH, GATE_SCRIPT.as_posix()], env=env,
                          capture_output=True, text=True, timeout=180)


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
    """Static: host and docker branches must both forward the transition flag."""
    text = GATE_SCRIPT.read_text(encoding="utf-8")
    assert text.count('"${GATE_FLAGS[@]}"') == 2, "both invocation paths must forward the flags"
    assert "--allow-pre-outbox-transition" in text
    assert 'GATE_FLAGS+=(--require-outbox)' in text
