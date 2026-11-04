"""Adversarial regressions for the PR #29 security review.

One test group per finding:

H1  counts are not evidence — identity and material state are compared
H2  deep upload verification is mandatory and fails closed when unavailable
H3  the live database is read in one consistent transaction; quiescence is enforced
H4  SQLite URIs are percent-encoded, so a ``#`` cannot redirect the check
M1  the live database identity is validated, not only the backup's
M2  non-finite freshness values are rejected
H5/H6  cold start requires proof; the migration runs after the backup gate
"""

from __future__ import annotations

import hashlib
import json
import os
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
import backup_safety  # noqa: E402
from backup_set_builder import (  # noqa: E402
    age_orders,
    backup_from_live,
    build_database,
)
from sqlite_uri import readonly_uri  # noqa: E402

NOW = datetime.now(timezone.utc)


# ------------------------------------------------------------------------- fixtures
def quiet_live(tmp_path: Path, *, with_outbox: bool = True) -> tuple[Path, Path]:
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
    return live_db, uploads


def snapshot_pair(tmp_path: Path, *, with_outbox: bool = True):
    live_db, uploads = quiet_live(tmp_path, with_outbox=with_outbox)
    backup, _ = backup_from_live(live_db, uploads, tmp_path / "backups" / "20260101T000000Z")
    return backup, live_db


def refresh_manifests(backup_dir: Path) -> None:
    db_sha = hashlib.sha256((backup_dir / "orders.db").read_bytes()).hexdigest()
    tar_sha = hashlib.sha256((backup_dir / "uploads.tar.gz").read_bytes()).hexdigest()
    (backup_dir / "manifest.sha256").write_text(
        f"{db_sha}  orders.db\n{tar_sha}  uploads.tar.gz\n", encoding="utf-8")
    payload = json.loads((backup_dir / "backup_manifest.json").read_text(encoding="utf-8"))
    payload["orders_db_sha256"] = db_sha
    payload["uploads_tar_sha256"] = tar_sha
    (backup_dir / "backup_manifest.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")


def mutate_backup_db(backup_dir: Path, sql: str, params=()) -> None:
    conn = sqlite3.connect(backup_dir / "orders.db")
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()
    refresh_manifests(backup_dir)


def run_gate(argv):
    return backup_gate.main(argv)


BASE = ["--require-outbox", "--max-age-hours", "48"]


def gate_args(backup: Path, live: Path, *extra) -> list:
    return ["--backup-dir", str(backup), "--live-db", str(live), *BASE, *extra]


# ------------------------------------------------------------------------------- H1
def test_same_order_count_but_different_order_identity_is_refused(tmp_path):
    """Five orders on both sides, but one id differs: the backup loses a real order."""
    backup, live = snapshot_pair(tmp_path)
    conn = sqlite3.connect(backup / "orders.db")
    try:
        conn.execute("DELETE FROM orders WHERE id=5")
        conn.execute("INSERT INTO orders (id, name, filename, payment_status, created_at) "
                     "VALUES (55, 'Impostor', 'x.jpg', 'unpaid', ?)",
                     ((NOW - timedelta(hours=2)).isoformat(),))
        conn.commit()
    finally:
        conn.close()
    refresh_manifests(backup)

    code = run_gate(gate_args(backup, live))
    assert code != 0


def test_same_counts_but_changed_payment_status_is_refused(tmp_path):
    """A backup that still says 'unpaid' would roll a completed payment back."""
    backup, live = snapshot_pair(tmp_path)
    mutate_backup_db(backup, "UPDATE orders SET payment_status='unpaid', paid_at=NULL "
                             "WHERE payment_status='paid'")

    code = run_gate(gate_args(backup, live))
    assert code == backup_gate.EXIT_UNVERIFIED


def test_same_job_count_but_replaced_job_identity_is_refused(tmp_path):
    backup, live = snapshot_pair(tmp_path)
    mutate_backup_db(backup,
                     "UPDATE delivery_jobs SET state=CASE WHEN state='delivered' "
                     "THEN 'pending' ELSE 'delivered' END")

    code = run_gate(gate_args(backup, live))
    assert code == backup_gate.EXIT_UNVERIFIED


def test_ambiguous_email_attempt_is_not_lost_by_restore(tmp_path):
    """live: failed/attempts=1 with an error; backup: pending/attempts=0.

    Equal row counts, materially different evidence — restoring the backup would
    erase the fact that an SMTP attempt was ambiguous.
    """
    backup, live = snapshot_pair(tmp_path)
    conn = sqlite3.connect(live)
    try:
        conn.execute(
            "UPDATE delivery_jobs SET state='failed', attempts=1, last_error='OperationalError' "
            "WHERE kind='paid_email'")
        conn.commit()
    finally:
        conn.close()

    code = run_gate(gate_args(backup, live))
    assert code == backup_gate.EXIT_UNVERIFIED


def test_outbox_state_change_after_the_backup_is_refused(tmp_path):
    """A post-backup outbox transition would be undone by a restore."""
    backup, live = snapshot_pair(tmp_path, with_outbox=True)
    conn = sqlite3.connect(live)
    try:
        # an attempt that became ambiguous after the backup was taken
        conn.execute("UPDATE paid_email_outbox SET state='uncertain', attempts=attempts+1 "
                     "WHERE order_id=(SELECT order_id FROM paid_email_outbox ORDER BY order_id LIMIT 1)")
        conn.commit()
    finally:
        conn.close()

    code = run_gate(gate_args(backup, live))
    assert code != 0


def test_a_faithful_snapshot_passes(tmp_path):
    """Control: the strict comparison must still accept a genuine snapshot."""
    backup, live = snapshot_pair(tmp_path)
    assert run_gate(gate_args(backup, live)) == 0


# ------------------------------------------------------------------------------- H2
def test_deep_verification_is_mandatory_in_both_wrapper_paths():
    text = (SCRIPTS_DIR / "deploy_backup_gate.sh").read_text(encoding="utf-8")
    host = text[text.index('GATE_MODE}" == "host"'):text.index("elif [[ \"${GATE_MODE}\" == \"docker\" ]]")]
    docker = text[text.index("elif [[ \"${GATE_MODE}\" == \"docker\" ]]"):]
    assert "--deep" in host, "host path must pass --deep"
    assert "--deep" in docker, "docker path must pass --deep"


def test_missing_deep_verifier_fails_closed(tmp_path, monkeypatch):
    backup, _ = snapshot_pair(tmp_path)
    # Make the verifier unimportable for this call.
    monkeypatch.setitem(sys.modules, "verify_backup_integrity", None)

    ok, report = backup_safety.verify_backup_set(
        backup, require_outbox=True, check_upload_archive=True)
    assert ok is False
    assert report.get("upload_archive_verified") is False
    assert any("deep upload verification is unavailable" in e for e in report["errors"])


def test_deep_rejects_an_archive_missing_a_required_upload(tmp_path):
    import io
    import tarfile

    backup, live = snapshot_pair(tmp_path)
    tar_path = backup / "uploads.tar.gz"
    with tarfile.open(tar_path, "r:gz") as tar:
        members = [(m, tar.extractfile(m).read() if m.isreg() else None)
                   for m in tar.getmembers()]
    victim = [m for m, _ in members if m.isreg() and m.name.endswith(".jpg")][0]
    with tarfile.open(tar_path, "w:gz") as tar:
        for member, data in members:
            if member.name == victim.name:
                continue
            tar.addfile(member, io.BytesIO(data) if data is not None else None)
    refresh_manifests(backup)

    assert run_gate(gate_args(backup, live, "--deep")) != 0


# ------------------------------------------------------------------------------- H3
def test_require_quiescent_without_a_database_is_a_configuration_error(tmp_path, capsys):
    backup, _ = snapshot_pair(tmp_path)
    code = run_gate(["--backup-dir", str(backup), *BASE, "--require-quiescent", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == backup_gate.EXIT_CONFIG
    assert any("cannot prove quiescence" in e for e in payload["errors"])


def test_a_processing_job_blocks_the_gate(tmp_path):
    """The job is in flight when the backup is taken, so evidence matches and only
    quiescence can reject it."""
    live_db, uploads = quiet_live(tmp_path)
    # Pick a file job whose source still exists, so the backup can archive it.
    conn = sqlite3.connect(live_db)
    try:
        candidates = conn.execute(
            "SELECT id, local_path FROM delivery_jobs WHERE kind='file' AND local_path IS NOT NULL"
        ).fetchall()
        target = next(job_id for job_id, local_path in candidates
                      if (uploads / Path(local_path).name).is_file())
        conn.execute("UPDATE delivery_jobs SET state='processing', lease_until=? WHERE id=?",
                     ((NOW + timedelta(minutes=3)).isoformat(), target))
        conn.commit()
    finally:
        conn.close()
    backup, _ = backup_from_live(live_db, uploads, tmp_path / "backups" / "20260101T000000Z")

    code = run_gate(gate_args(backup, live_db, "--require-quiescent", "--worker-enabled", "no"))
    assert code == backup_gate.EXIT_NOT_QUIESCENT


def test_enabled_worker_blocks_the_gate(tmp_path):
    backup, live = snapshot_pair(tmp_path)
    code = run_gate(gate_args(backup, live, "--require-quiescent", "--worker-enabled", "yes"))
    assert code == backup_gate.EXIT_NOT_QUIESCENT


def test_live_database_is_read_once_per_check(tmp_path, monkeypatch):
    """One transaction per check: schema and rows must come from the same snapshot.

    The original finding was that outbox presence and the row counts were read
    through different connections, so a table created in between was invisible.
    """
    backup, live = snapshot_pair(tmp_path)
    opened: list[Path] = []
    real = backup_gate._open_ro

    def counting(path):
        opened.append(Path(path))
        return real(path)

    monkeypatch.setattr(backup_gate, "_open_ro", counting)
    # transition mode implies "no outbox requirement", hence a clean argument list
    run_gate(["--backup-dir", str(backup), "--live-db", str(live),
              "--allow-pre-outbox-transition", "--max-age-hours", "48"])

    live_opens = [p for p in opened if p.resolve() == Path(live).resolve()]
    assert len(live_opens) == 1, f"live database opened {len(live_opens)} times"


# ------------------------------------------------------------------------------- H4
def test_readonly_uri_encodes_dangerous_characters():
    uri = readonly_uri("/tmp/a#b?c d%e ru.db")
    assert "#" not in uri.split("?")[0]
    assert "%23" in uri and "%3F" in uri and "%20" in uri and "%25" in uri
    assert uri.endswith("?mode=ro")


def test_uri_fragment_cannot_redirect_the_live_check(tmp_path):
    """``identity.db#real-live`` must not be read as ``identity.db``."""
    from paid_email_outbox import EMAIL_SCHEMA

    base = tmp_path / "identity.db"
    live_with_outbox = tmp_path / "identity.db#real-live"

    # the decoy: a database WITHOUT the outbox at the truncated path
    build_database(base, tmp_path / "decoy-uploads", with_outbox=False)
    # the real target: WITH the outbox
    build_database(live_with_outbox, tmp_path / "real-uploads", with_outbox=True)
    conn = sqlite3.connect(live_with_outbox)
    try:
        conn.executescript(EMAIL_SCHEMA)
        conn.commit()
    finally:
        conn.close()

    backup, _ = snapshot_pair(tmp_path)

    code = run_gate(["--backup-dir", str(backup), "--live-db", str(live_with_outbox),
                     "--allow-pre-outbox-transition", "--max-age-hours", "48"])
    assert code != 0, "the gate followed the fragment and inspected the decoy database"


def test_uri_cannot_create_a_database(tmp_path):
    """A missing path must be reported, not created as a side effect."""
    missing = tmp_path / "does-not-exist.db#frag"
    backup, _ = snapshot_pair(tmp_path)

    run_gate(["--backup-dir", str(backup), "--live-db", str(missing), *BASE])
    assert not missing.exists()
    assert not (tmp_path / "does-not-exist.db").exists()


# ------------------------------------------------------------------------------- M1
def test_live_database_identity_is_validated(tmp_path, capsys):
    backup, live = snapshot_pair(tmp_path)
    conn = sqlite3.connect(live)
    try:
        conn.execute("PRAGMA application_id=0")
        conn.commit()
    finally:
        conn.close()

    code = run_gate(["--backup-dir", str(backup), "--live-db", str(live), *BASE, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == backup_gate.EXIT_UNVERIFIED
    assert any("live database application_id" in e for e in payload["errors"])


def test_corrupt_live_database_is_refused(tmp_path):
    backup, live = snapshot_pair(tmp_path)
    with live.open("r+b") as handle:
        handle.seek(0)
        handle.write(b"NOT-A-DATABASE-AT-ALL")
    assert run_gate(gate_args(backup, live)) != 0


# ------------------------------------------------------------------------------- M2
@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "-inf", "Infinity", "-1", "0",
                                   "abc", "", "1e999"])
def test_nonfinite_or_invalid_age_is_rejected(tmp_path, capsys, value):
    backup, _ = snapshot_pair(tmp_path)
    # ``--flag=value`` so a leading '-' is not parsed as another option
    code = run_gate(["--backup-dir", str(backup), f"--max-age-hours={value}", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == backup_gate.EXIT_CONFIG, f"{value!r} was accepted"
    assert payload["errors"]


def test_nan_age_does_not_disable_freshness_on_the_wrapper(tmp_path):
    """The wrapper rejects the value before the gate can be bypassed with NaN."""
    backup, live = snapshot_pair(tmp_path)
    env = dict(os.environ)
    env.update({
        "REPO_ROOT": REPO_DIR.as_posix(), "PYTHON_BIN": sys.executable,
        "GATE_MODE": "host", "BACKUP_ROOT": (backup.parent).as_posix(),
        "LIVE_DB": Path(live).as_posix(), "BACKUP_MAX_AGE_HOURS": "nan",
    })
    bash = next((c for c in (r"C:\Program Files\Git\usr\bin\bash.exe",
                             r"C:\Program Files\Git\bin\bash.exe",
                             "/bin/bash", "/usr/bin/bash") if Path(c).is_file()), None)
    if bash is None:
        pytest.skip("bash unavailable")
    if "Git" in bash:
        git_root = Path(bash).parent.parent
        env["PATH"] = os.pathsep.join([str(git_root / "usr" / "bin"),
                                       str(git_root / "bin"), env.get("PATH", "")])
    result = subprocess.run([bash, (SCRIPTS_DIR / "deploy_backup_gate.sh").as_posix()],
                            env=env, capture_output=True, text=True, timeout=240)
    assert result.returncode == 1
    assert "BACKUP_MAX_AGE_HOURS" in result.stderr


# ---------------------------------------------------------------------------- H5/H6
def test_cold_start_requires_explicit_proof():
    text = (SCRIPTS_DIR / "deploy_backup_gate.sh").read_text(encoding="utf-8")
    assert "DB_ABSENCE_PROVEN" in text
    assert "cold start refused" in text
    # the executable skip branch must be resolved before the cold-start case
    skip_branch = text.index('if [[ "${SKIP_BACKUP_GATE:-0}" == "1" ]]; then')
    cold_case = text.index("    cold)")
    assert skip_branch < cold_case, "SKIP must be resolved before cold-start reasoning"


def test_deployment_probes_presence_as_root_and_runs_gate_before_migration():
    text = (SCRIPTS_DIR / "deploy_staging_prebuilt.sh").read_text(encoding="utf-8")
    gate_pos = text.index("deploy_backup_gate.sh")
    apply_pos = text.index("reconcile_delivery_schema.py --db /data/orders.db --apply")
    assert gate_pos < apply_pos, "the migration must not run before the backup gate"
    # presence detection must not run unprivileged, where EACCES looks like absence
    probe = text[text.index("[7a/10]"):text.index("[7b/10]")]
    assert "--user 0" in probe
    assert "DB_ABSENCE_PROVEN=1" in probe
    assert "Refusing to treat an unreadable volume as a cold start" in probe
