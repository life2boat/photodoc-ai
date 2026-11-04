"""Third-pass adversarial regressions for PR #29.

One group per finding of the second independent review:

F1  incomplete field comparison / application-id-only identity
F2  quiescence without continuous writer exclusion
F3  docker inspection failure read as proven absence
F4  confirmation after migration
F5  the documented quiescence command could not run
F6  `info` jobs misdescribed as file-less
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
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
import writer_barrier  # noqa: E402
from backup_set_builder import (  # noqa: E402
    age_orders,
    backup_from_live,
    build_database,
)

NOW = datetime.now(timezone.utc)
DEPLOY_SCRIPT = SCRIPTS_DIR / "deploy_staging_prebuilt.sh"
GATE_WRAPPER = SCRIPTS_DIR / "deploy_backup_gate.sh"


# ------------------------------------------------------------------------- fixtures
def quiet_pair(tmp_path: Path, *, with_outbox: bool = True):
    live_db = tmp_path / "live" / "orders.db"
    uploads = tmp_path / "live" / "uploads"
    build_database(live_db, uploads, with_outbox=with_outbox)
    age_orders(live_db, NOW - timedelta(hours=1))
    conn = sqlite3.connect(live_db)
    try:
        conn.execute("UPDATE delivery_jobs SET state='pending', lease_until=NULL "
                     "WHERE state='processing'")
        conn.commit()
    finally:
        conn.close()
    backup, _ = backup_from_live(live_db, uploads, tmp_path / "backups" / "20260101T000000Z")
    return backup, live_db


def refresh(backup_dir: Path) -> None:
    db_sha = hashlib.sha256((backup_dir / "orders.db").read_bytes()).hexdigest()
    tar_sha = hashlib.sha256((backup_dir / "uploads.tar.gz").read_bytes()).hexdigest()
    (backup_dir / "manifest.sha256").write_text(
        f"{db_sha}  orders.db\n{tar_sha}  uploads.tar.gz\n", encoding="utf-8")
    payload = json.loads((backup_dir / "backup_manifest.json").read_text(encoding="utf-8"))
    payload["orders_db_sha256"] = db_sha
    payload["uploads_tar_sha256"] = tar_sha
    (backup_dir / "backup_manifest.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")


def mutate(path: Path, sql: str, params=()) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def args(backup: Path, live: Path, *extra) -> list:
    return ["--backup-dir", str(backup), "--live-db", str(live),
            "--require-outbox", "--max-age-hours", "48", *extra]


# ------------------------------------------------------------------------------- F1
@pytest.mark.parametrize("column,value", [
    ("order_amount", "999.00"),
    ("payment_amount", "1.00"),
    ("name", "Someone Else"),
    ("email", "attacker@example.invalid"),
    ("phone", "+70000000000"),
])
def test_changed_order_field_is_rejected(tmp_path, column, value):
    """Every persisted order field is evidence: a changed price or recipient must
    block, not merely a changed row count."""
    backup, live = quiet_pair(tmp_path)
    mutate(live, f"UPDATE orders SET {column}=? WHERE payment_status='paid' "
                 "AND id=(SELECT MIN(id) FROM orders WHERE payment_status='paid')", (value,))

    assert backup_gate.main(args(backup, live)) != 0


def test_changed_delivery_remote_path_is_rejected(tmp_path):
    """A different delivery destination in live means the backup would re-send to
    the old path."""
    backup, live = quiet_pair(tmp_path)
    mutate(live, "UPDATE delivery_jobs SET remote_path=? WHERE kind='file' "
                 "AND id=(SELECT MIN(id) FROM delivery_jobs WHERE kind='file')",
           ("PhotoDoc_Orders/elsewhere/synthetic.jpg",))

    assert backup_gate.main(args(backup, live)) != 0


def test_changed_cleanup_metadata_is_rejected(tmp_path):
    backup, live = quiet_pair(tmp_path)
    mutate(live, "UPDATE delivery_jobs SET cleanup_state='completed', local_deleted_at=? "
                 "WHERE id=(SELECT MIN(id) FROM delivery_jobs)", (NOW.isoformat(),))

    assert backup_gate.main(args(backup, live)) != 0


def test_extra_column_in_the_backup_is_rejected_as_unexpected_schema(tmp_path):
    backup, live = quiet_pair(tmp_path)
    mutate(backup / "orders.db", "ALTER TABLE orders ADD COLUMN injected TEXT")
    refresh(backup)

    code = backup_gate.main(args(backup, live))
    assert code == backup_gate.EXIT_UNVERIFIED


def test_missing_table_in_the_backup_is_rejected(tmp_path):
    backup, live = quiet_pair(tmp_path)
    mutate(backup / "orders.db", "DROP TABLE delivery_jobs")
    refresh(backup)

    assert backup_gate.main(args(backup, live)) != 0


def test_extra_table_in_live_is_rejected(tmp_path):
    backup, live = quiet_pair(tmp_path)
    mutate(live, "CREATE TABLE unexpected_state (id INTEGER PRIMARY KEY, v TEXT)")

    assert backup_gate.main(args(backup, live)) != 0


def test_a_faithful_snapshot_still_passes(tmp_path):
    backup, live = quiet_pair(tmp_path)
    assert backup_gate.main(args(backup, live)) == 0


# ------------------------------------------------------------------------------- F2
def test_unknown_worker_state_is_not_treated_as_quiescent(tmp_path):
    backup, live = quiet_pair(tmp_path)
    code = backup_gate.main(args(backup, live, "--require-quiescent",
                                 "--worker-enabled", "unknown"))
    assert code == backup_gate.EXIT_NOT_QUIESCENT


def test_separate_db_pointing_at_a_quiet_decoy_is_rejected(tmp_path):
    """A quiet --db must not be able to vouch for a busy --live-db."""
    backup, live = quiet_pair(tmp_path)
    decoy = tmp_path / "decoy" / "orders.db"
    build_database(decoy, tmp_path / "decoy" / "uploads", with_outbox=True)
    mutate(decoy, "UPDATE delivery_jobs SET state='delivered'")

    code = backup_gate.main(args(backup, live, "--db", str(decoy), "--require-quiescent",
                                 "--worker-enabled", "no"))
    assert code == backup_gate.EXIT_CONFIG


def test_quiescence_is_derived_from_the_comparison_snapshot(tmp_path, capsys):
    backup, live = quiet_pair(tmp_path)
    code = backup_gate.main(args(backup, live, "--require-quiescent",
                                 "--worker-enabled", "no", "--json"))
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload.get("errors")
    quiet = payload["checks"]["quiescence"]
    assert "same snapshot" in quiet["source"]
    assert quiet["processing_jobs"] == payload["checks"]["live_database"]["processing_jobs"]


def test_a_write_after_the_gate_is_detected_by_the_fingerprint(tmp_path):
    """The gate cannot hold writers down; the barrier's fingerprint check is what
    catches a write in the window before the migration."""
    backup, live = quiet_pair(tmp_path)
    snap, errors = backup_gate._live_snapshot(live)
    assert not errors
    approved = writer_barrier.fingerprint(live)

    # a new order arrives after the gate approved the backup
    mutate(live, "INSERT INTO orders (id, name, filename, payment_status, created_at) "
                 "VALUES (999, 'Late', 'late.jpg', 'unpaid', ?)", (NOW.isoformat(),))

    code, report = writer_barrier.evaluate(live, volume=None, allow_unsupported=True,
                                           expect_fingerprint=approved)
    assert code == writer_barrier.EXIT_BLOCKED
    assert report["reason"] == "database-changed"


@pytest.mark.skipif(shutil.which("bash") is None and not Path(r"C:\Program Files\Git\usr\bin\bash.exe").is_file(),
                    reason="bash unavailable")
def test_wrapper_refuses_an_unknown_worker_state(tmp_path):
    backup, live = quiet_pair(tmp_path)
    env = _bash_env(backup.parent, live)
    env.pop("DELIVERY_WORKER_ENABLED_STATE", None)
    result = _run_wrapper(env)
    assert result.returncode == 1
    assert "DELIVERY_WORKER_ENABLED_STATE" in result.stderr


@pytest.mark.skipif(shutil.which("bash") is None and not Path(r"C:\Program Files\Git\usr\bin\bash.exe").is_file(),
                    reason="bash unavailable")
def test_wrapper_refuses_an_enabled_worker(tmp_path):
    backup, live = quiet_pair(tmp_path)
    env = _bash_env(backup.parent, live, DELIVERY_WORKER_ENABLED_STATE="yes")
    result = _run_wrapper(env)
    assert result.returncode == 1
    assert "worker is enabled" in result.stderr


# ------------------------------------------------------------------------------- F3
def _bash() -> str | None:
    for candidate in (r"C:\Program Files\Git\usr\bin\bash.exe",
                      r"C:\Program Files\Git\bin\bash.exe", "/bin/bash", "/usr/bin/bash"):
        if Path(candidate).is_file():
            return candidate
    return shutil.which("bash")


BASH = _bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="bash unavailable")


def _bash_env(backup_root: Path, live: Path, **overrides) -> dict:
    env = dict(os.environ)
    env.update({
        "REPO_ROOT": REPO_DIR.as_posix(),
        "PYTHON_BIN": sys.executable,
        "GATE_MODE": "host",
        "BACKUP_ROOT": Path(backup_root).as_posix(),
        "LIVE_DB": Path(live).as_posix(),
        "BACKUP_MAX_AGE_HOURS": "48",
        "DELIVERY_WORKER_ENABLED_STATE": "no",
    })
    env.pop("SKIP_BACKUP_GATE", None)
    env.pop("DB_ABSENCE_PROVEN", None)
    env.update(overrides)
    if BASH and "Git" in BASH:
        git_root = Path(BASH).parent.parent
        env["PATH"] = os.pathsep.join(
            [str(git_root / "usr" / "bin"), str(git_root / "bin"), env.get("PATH", "")])
    return env


def _run_wrapper(env: dict, stub_dir: Path | None = None) -> subprocess.CompletedProcess:
    if stub_dir is not None:
        env = dict(env)
        env["PATH"] = os.pathsep.join([str(stub_dir), env.get("PATH", "")])
    return subprocess.run([BASH, GATE_WRAPPER.as_posix()], env=env,
                          capture_output=True, text=True, timeout=240)


def _stub_docker(tmp_path: Path, *, info_rc: int, inspect_rc: int, inspect_msg: str) -> Path:
    """A fake ``docker`` on PATH: the wrapper must interpret its answers correctly."""
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "docker"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  info) exit %d ;;\n"
        "  volume)\n"
        '    printf "%%s\\n" "%s" >&2\n'
        "    exit %d ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n" % (info_rc, inspect_msg, inspect_rc),
        encoding="utf-8")
    script.chmod(0o755)
    return bin_dir


def test_cold_start_refused_when_the_daemon_is_unreachable(tmp_path):
    stub = _stub_docker(tmp_path, info_rc=1, inspect_rc=1, inspect_msg="Cannot connect to the Docker daemon")
    env = _bash_env(tmp_path / "backups", tmp_path / "none.db",
                    GATE_MODE="cold", DB_ABSENCE_PROVEN="1", DB_VOLUME="vol")
    result = _run_wrapper(env, stub_dir=stub)
    assert result.returncode == 1
    assert "daemon is unreachable" in result.stderr


def test_cold_start_refused_on_a_permission_error(tmp_path):
    stub = _stub_docker(tmp_path, info_rc=0, inspect_rc=1,
                        inspect_msg="permission denied while trying to connect")
    env = _bash_env(tmp_path / "backups", tmp_path / "none.db",
                    GATE_MODE="cold", DB_ABSENCE_PROVEN="1", DB_VOLUME="vol")
    result = _run_wrapper(env, stub_dir=stub)
    assert result.returncode == 1
    assert "cannot distinguish an absent volume from an error" in result.stderr


def test_cold_start_allowed_only_on_no_such_volume(tmp_path):
    stub = _stub_docker(tmp_path, info_rc=0, inspect_rc=1,
                        inspect_msg="Error: No such volume: vol")
    env = _bash_env(tmp_path / "backups", tmp_path / "none.db",
                    GATE_MODE="cold", DB_ABSENCE_PROVEN="1", DB_VOLUME="vol",
                    DB_ABSENCE_EVIDENCE="volume-not-found")
    result = _run_wrapper(env, stub_dir=stub)
    assert result.returncode == 0, result.stderr
    assert "Cold start proven" in result.stderr


def test_cold_start_refused_when_the_volume_exists(tmp_path):
    stub = _stub_docker(tmp_path, info_rc=0, inspect_rc=0, inspect_msg="[]")
    env = _bash_env(tmp_path / "backups", tmp_path / "none.db",
                    GATE_MODE="cold", DB_ABSENCE_PROVEN="1", DB_VOLUME="vol")
    result = _run_wrapper(env, stub_dir=stub)
    assert result.returncode == 1
    assert "cannot be proven" in result.stderr or "exists" in result.stderr


def test_cold_start_refused_when_live_db_contradicts_the_claim(tmp_path):
    backup, live = quiet_pair(tmp_path)
    env = _bash_env(backup.parent, live, GATE_MODE="cold", DB_ABSENCE_PROVEN="1")
    result = _run_wrapper(env)
    assert result.returncode == 1
    assert "contradicts" in result.stderr


def test_deploy_script_distinguishes_docker_inspection_failures():
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    probe = text[text.index("[7a/10]"):text.index("[7b/10]")]
    assert "docker info" in probe, "the daemon must be proven reachable before absence is inferred"
    assert "no such volume" in probe, "only the specific not-found error may prove absence"
    assert "is not proof of absence" in probe
    assert "DB_ABSENCE_EVIDENCE" in probe


# ------------------------------------------------------------------------------- F4
def test_confirmation_precedes_the_first_mutation():
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    prompt = text.index("Apply staging container update now?")
    apply_pos = text.index("reconcile_delivery_schema.py --db /data/orders.db --apply")
    assert prompt < apply_pos, "operator approval must precede the schema migration"
    assert "Nothing has been changed" in text, "a refusal must state that state is untouched"


def test_migration_is_rechecked_against_the_approved_fingerprint():
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    apply_pos = text.index("reconcile_delivery_schema.py --db /data/orders.db --apply")
    recheck_pos = text.index("--expect-fingerprint")
    assert recheck_pos < apply_pos, "exclusion and identity must be re-proved before the write"


# ------------------------------------------------------------------------------- F5
def test_quiescence_only_mode_works_standalone(tmp_path):
    _, live = quiet_pair(tmp_path)
    code = backup_gate.main(["--quiescence-only", "--db", str(live),
                             "--worker-enabled", "no", "--require-exclusive-lock"])
    assert code == 0


def test_quiescence_only_without_a_database_is_a_config_error():
    assert backup_gate.main(["--quiescence-only"]) == backup_gate.EXIT_CONFIG


def test_documented_quiescence_command_is_executable():
    """The runbook must not instruct a command that cannot run."""
    docs = (REPO_DIR / "docs" / "BACKUP_AND_RESTORE.md").read_text(encoding="utf-8")
    assert "--quiescence-only" in docs
    assert "writer_barrier.py" in docs
    for line in docs.splitlines():
        stripped = line.strip()
        if "backup_gate.py" in stripped and "--require-quiescent" in stripped:
            # a gate invocation that demands quiescence must also supply a backup
            # set or opt into the standalone mode
            assert ("--backup-dir" in stripped or "--latest-of" in stripped
                    or "--quiescence-only" in stripped), stripped


# ------------------------------------------------------------------------------- F6
def test_docs_do_not_describe_info_jobs_as_file_less():
    """``info`` jobs carry a local file; only email jobs do not."""
    text = (REPO_DIR / "docs" / "DEPLOYMENT_BLOCK_REVIEW.md").read_text(encoding="utf-8")
    assert "`info` jobs *are* handled as file-backed" in text
    assert "such as `info`/`admin_email`" not in text
    assert "such as `admin_email`/`paid_email`" in text
