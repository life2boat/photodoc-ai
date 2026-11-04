"""Deployment backup gate (shell wrapper) — refuses unsafe cutovers.

Phase 6 requires the gate to be testable locally with synthetic fixtures, so the
decision logic lives in ``scripts/deploy_backup_gate.sh`` and is exercised here
with real files and no Docker.

The last two tests are static regressions: the deployment script must still call
the gate, and the unsafe ``cp`` of a live database must not come back.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
SCRIPTS_DIR = REPO_DIR / "scripts"
GATE_SCRIPT = SCRIPTS_DIR / "deploy_backup_gate.sh"
DEPLOY_SCRIPT = SCRIPTS_DIR / "deploy_staging_prebuilt.sh"
for _p in (str(BACKEND_DIR), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backup_set_builder import build_database, make_backup_set  # noqa: E402

NOW = datetime.now(timezone.utc)


def _bash() -> str | None:
    """Locate a bash that can read this working tree.

    On Windows the first ``bash`` on PATH is often the WSL launcher stub, which
    cannot open a native Windows path (and fails outright on a non-ASCII one), so
    the real MSYS/Git bash is preferred and the PATH lookup is only a fallback.
    """
    for candidate in (
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files\Git\bin\bash.exe",
        "/bin/bash",
        "/usr/bin/bash",
    ):
        if Path(candidate).is_file():
            return candidate
    return shutil.which("bash")


BASH = _bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available")


def _age_live_orders(db_path: Path, when: datetime) -> None:
    """Make every order older than ``when`` so a backup at ``when`` covers them."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE orders SET created_at=?", (when.isoformat(),))
        conn.commit()
    finally:
        conn.close()


def _posix(path: Path) -> str:
    return Path(path).as_posix()


def run_gate(tmp_path: Path, **env_overrides) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({
        "REPO_ROOT": _posix(REPO_DIR),
        "PYTHON_BIN": sys.executable,
        "GATE_MODE": "host",
        "DELIVERY_WORKER_ENABLED_STATE": "no",
    })
    env.pop("SKIP_BACKUP_GATE", None)
    # A Git-bash subprocess inherits the Windows PATH, where coreutils such as
    # dirname are not resolvable; add the MSYS bin directories for this call.
    if BASH and "Git" in BASH:
        git_root = Path(BASH).parent.parent  # .../Git
        extra = [str(git_root / "usr" / "bin"), str(git_root / "bin")]
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    env.update({key: _posix(Path(value)) if isinstance(value, Path) else str(value)
                for key, value in env_overrides.items()})
    return subprocess.run([BASH, _posix(GATE_SCRIPT)], env=env,
                          capture_output=True, text=True, timeout=180)


def _quiet_live(tmp_path: Path, *, with_outbox: bool = True):
    """A live database plus a backup taken of it, with no writer in flight."""
    from backup_set_builder import backup_from_live

    live_db = tmp_path / "live" / "orders.db"
    uploads = tmp_path / "live" / "uploads"
    build_database(live_db, uploads, with_outbox=with_outbox)
    _age_live_orders(live_db, NOW - timedelta(hours=1))
    import sqlite3
    conn = sqlite3.connect(live_db)
    try:
        conn.execute("UPDATE delivery_jobs SET state='pending', lease_until=NULL "
                     "WHERE state='processing'")
        conn.commit()
    finally:
        conn.close()
    backups = tmp_path / "backups"
    backup, _ = backup_from_live(live_db, uploads, backups / "20260101T000000Z")
    return backups, live_db


def test_cold_start_passes_only_when_absence_is_proven(tmp_path):
    result = run_gate(tmp_path, GATE_MODE="cold", DB_ABSENCE_PROVEN="1")
    assert result.returncode == 0, result.stderr
    assert "Cold start proven" in result.stderr


def test_cold_start_without_proof_is_refused(tmp_path):
    """An unproven cold start could hide an unreadable database behind 'empty'."""
    result = run_gate(tmp_path, GATE_MODE="cold")
    assert result.returncode == 1, result.stderr
    assert "cold start refused" in result.stderr


def test_auto_mode_without_any_configuration_is_refused(tmp_path):
    """No LIVE_DB and no DB_VOLUME must be an error, never a silent cold start."""
    result = run_gate(tmp_path, GATE_MODE="auto")
    assert result.returncode == 1, result.stderr
    assert "cannot determine the database" in result.stderr


def test_host_mode_with_a_missing_live_db_is_refused(tmp_path):
    result = run_gate(tmp_path, GATE_MODE="host", LIVE_DB=tmp_path / "absent.db")
    assert result.returncode == 1, result.stderr
    assert "not a readable file" in result.stderr


def test_skip_is_resolved_before_cold_start(tmp_path):
    """SKIP must be honoured (exit 2) even when the mode would resolve to cold."""
    result = run_gate(tmp_path, GATE_MODE="cold", SKIP_BACKUP_GATE="1")
    assert result.returncode == 2, result.stderr
    assert "SKIP_BACKUP_GATE=1" in result.stderr


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-1", "abc", "24; touch /tmp/x",
                                   "1e999", "0.0"])
def test_invalid_age_value_is_refused(tmp_path, value):
    """A NaN threshold would silently disable freshness; reject it in the wrapper."""
    result = run_gate(tmp_path, GATE_MODE="cold", DB_ABSENCE_PROVEN="1",
                      BACKUP_MAX_AGE_HOURS=value)
    assert result.returncode == 1, f"{value!r} should be rejected: {result.stderr}"
    assert "BACKUP_MAX_AGE_HOURS" in result.stderr


def test_empty_age_value_falls_back_to_the_default(tmp_path):
    """An unset/empty variable means 'default', not 'no limit'."""
    result = run_gate(tmp_path, GATE_MODE="cold", DB_ABSENCE_PROVEN="1",
                      BACKUP_MAX_AGE_HOURS="")
    assert result.returncode == 0, result.stderr


def test_passes_with_a_current_verified_backup(tmp_path):
    backups, live_db = _quiet_live(tmp_path)
    result = run_gate(tmp_path, BACKUP_ROOT=backups, LIVE_DB=live_db)
    assert result.returncode == 0, result.stderr
    assert "gate passed" in result.stderr.lower()


def test_refuses_when_the_backup_root_does_not_exist(tmp_path):
    live = tmp_path / "live"
    build_database(live / "orders.db", live / "uploads", with_outbox=True)

    result = run_gate(tmp_path, BACKUP_ROOT=tmp_path / "no-such-root",
                      LIVE_DB=live / "orders.db")
    assert result.returncode == 1
    assert "FATAL" in result.stderr


def test_refuses_when_the_backup_directory_is_empty(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    live = tmp_path / "live"
    build_database(live / "orders.db", live / "uploads", with_outbox=True)

    result = run_gate(tmp_path, BACKUP_ROOT=backups, LIVE_DB=live / "orders.db")
    assert result.returncode == 1
    assert "refused this deployment" in result.stderr


def test_refuses_an_unverified_backup(tmp_path):
    backups = tmp_path / "backups"
    make_backup_set(backups, verification="FAIL", created=NOW)
    live = tmp_path / "live"
    build_database(live / "orders.db", live / "uploads", with_outbox=True)

    result = run_gate(tmp_path, BACKUP_ROOT=backups, LIVE_DB=live / "orders.db")
    assert result.returncode == 1


def test_refuses_a_backup_older_than_the_live_data(tmp_path):
    """The decisive case: valid backup, but it predates the newest order."""
    backups = tmp_path / "backups"
    make_backup_set(backups, created=NOW - timedelta(hours=12))
    live = tmp_path / "live"
    build_database(live / "orders.db", live / "uploads", with_outbox=True)
    import sqlite3
    conn = sqlite3.connect(live / "orders.db")
    try:
        conn.execute("UPDATE orders SET created_at=?", ((NOW + timedelta(minutes=5)).isoformat(),))
        conn.commit()
    finally:
        conn.close()

    result = run_gate(tmp_path, BACKUP_ROOT=backups, LIVE_DB=live / "orders.db",
                      BACKUP_MAX_AGE_HOURS="48")
    assert result.returncode == 1, result.stdout
    assert "refused this deployment" in result.stderr


def test_skip_override_is_loud_and_distinct(tmp_path):
    live = tmp_path / "live"
    build_database(live / "orders.db", live / "uploads", with_outbox=True)

    result = run_gate(tmp_path, BACKUP_ROOT=tmp_path / "backups",
                      LIVE_DB=live / "orders.db", SKIP_BACKUP_GATE="1")
    assert result.returncode == 2, "an explicit skip must be distinguishable from a pass"
    assert "WARNING" in result.stderr
    assert "WITHOUT a verified backup" in result.stderr


# ------------------------------------------------------------ static regressions
def test_deployment_script_calls_the_gate_before_starting_containers():
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "deploy_backup_gate.sh" in text, "the deployment no longer consults the gate"
    gate_pos = text.index("deploy_backup_gate.sh")
    start_pos = text.index("Starting Prebuilt Staging Containers")
    assert gate_pos < start_pos, "the gate must run before containers are started"
    assert "Refusing to touch running data" in text


def test_deployment_script_no_longer_copies_a_live_database():
    """`cp` of a live SQLite file was the S6-01 defect; it must not return."""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "cp /data/orders.db" not in stripped, (
            f"unsafe live-database copy reintroduced: {stripped}")


def test_release_runbook_uses_the_backup_tool_not_cp():
    runbook = (REPO_DIR / "docs" / "PRODUCTION_RELEASE.md").read_text(encoding="utf-8")
    assert "create_backup.py" in runbook, "the runbook must use the real backup tool"
    assert "backup_gate.py" in runbook, "the runbook must verify via the gate"
    for line in runbook.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith(">"):
            continue
        assert "cp /data/orders.db" not in stripped, (
            f"runbook still copies a live database: {stripped}")
