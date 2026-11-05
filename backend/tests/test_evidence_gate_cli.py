"""End-to-end tests for the Stage 1 evidence gate CLI.

These exercise the real command line (subprocess), not just the helper functions,
so argument handling, exit codes and the structured report are covered together.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from evidence_fixtures import APPLICATION_ID, NOW, build_database, copy_database, make_set

import evidence_gate

REPO_DIR = Path(__file__).resolve().parents[2]
CLI = REPO_DIR / "scripts" / "evidence_gate.py"


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CLI), *args],
                          capture_output=True, text=True, timeout=300)


@pytest.fixture()
def live(tmp_path: Path) -> Path:
    return build_database(tmp_path / "live.db")


@pytest.fixture()
def backup_set(tmp_path, live) -> Path:
    return make_set(live, tmp_path / "backups" / "20260101T000000Z")


# ------------------------------------------------------------------------ happy path
def test_a_matching_set_passes(live, backup_set):
    result = run_cli("--live-db", str(live), "--backup-dir", str(backup_set), "--restore-uploads-root", "/app/uploads", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["checks"]["evidence"]["schema_digest"] == \
        payload["checks"]["evidence"]["backup_schema_digest"]
    assert payload["checks"]["evidence"]["tables"]


def test_the_human_output_names_the_evidence(live, backup_set):
    result = run_cli("--live-db", str(live), "--backup-dir", str(backup_set), "--restore-uploads-root", "/app/uploads")
    assert result.returncode == 0
    assert "GATE: PASS" in result.stdout
    assert "sqlite_sequence" in result.stdout


# ----------------------------------------------------------------------------- refusals
def test_a_changed_row_is_refused(tmp_path, live, backup_set):
    changed = build_database(tmp_path / "changed.db", variant="wr_row")
    result = run_cli("--live-db", str(changed), "--backup-dir", str(backup_set), "--restore-uploads-root", "/app/uploads", "--json")
    assert result.returncode == evidence_gate.EXIT_INVALID
    payload = json.loads(result.stdout)
    assert any("wr_items" in reason for reason in payload["errors"])


def test_an_extra_backup_row_is_refused(tmp_path, live):
    extra = build_database(tmp_path / "extra.db", variant="backup_only_row")
    path = make_set(extra, tmp_path / "backups" / "s1")
    result = run_cli("--live-db", str(live), "--backup-dir", str(path), "--restore-uploads-root", "/app/uploads", "--json")
    assert result.returncode == evidence_gate.EXIT_INVALID
    assert any("resurrect" in reason for reason in json.loads(result.stdout)["errors"])


def test_a_stale_set_is_refused(live, tmp_path):
    old = make_set(live, tmp_path / "backups" / "old",
                   created_at=NOW - timedelta(hours=100))
    result = run_cli("--live-db", str(live), "--backup-dir", str(old), "--restore-uploads-root", "/app/uploads",
                     "--max-age-hours", "24", "--json")
    assert result.returncode == evidence_gate.EXIT_STALE


def test_a_future_timestamp_is_refused(live, tmp_path):
    future = make_set(live, tmp_path / "backups" / "future",
                      created_at=NOW + timedelta(hours=3))
    result = run_cli("--live-db", str(live), "--backup-dir", str(future), "--restore-uploads-root", "/app/uploads", "--json")
    assert result.returncode == evidence_gate.EXIT_INVALID
    assert any("future" in reason for reason in json.loads(result.stdout)["errors"])


def test_a_tampered_manifest_is_refused(live, backup_set):
    path = backup_set / "backup_manifest.json"
    manifest = json.loads(path.read_text())
    manifest["verification"] = "SKIPPED"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    result = run_cli("--live-db", str(live), "--backup-dir", str(backup_set), "--restore-uploads-root", "/app/uploads", "--json")
    assert result.returncode == evidence_gate.EXIT_INVALID
    assert any("backup_manifest.json" in reason
               for reason in json.loads(result.stdout)["errors"])


# ------------------------------------------------------------------- policy handling
def test_pre_outbox_policy_passes_when_neither_side_has_the_table(live, backup_set):
    result = run_cli("--live-db", str(live), "--backup-dir", str(backup_set), "--restore-uploads-root", "/app/uploads",
                     "--policy", "pre-outbox-transition", "--json")
    assert result.returncode == 0, result.stdout


def test_pre_outbox_policy_is_refused_once_the_table_exists(tmp_path, live):
    conn = sqlite3.connect(live)
    try:
        conn.execute("CREATE TABLE paid_email_outbox (id INTEGER PRIMARY KEY, state TEXT)")
        conn.execute("INSERT INTO paid_email_outbox (state) VALUES ('pending')")
        conn.commit()
    finally:
        conn.close()
    path = make_set(live, tmp_path / "backups" / "s1")
    result = run_cli("--live-db", str(live), "--backup-dir", str(path), "--restore-uploads-root", "/app/uploads",
                     "--policy", "pre-outbox-transition", "--json")
    assert result.returncode == evidence_gate.EXIT_INVALID
    assert any("paid_email_outbox" in reason for reason in json.loads(result.stdout)["errors"])


def test_an_unknown_policy_is_a_config_error(live, backup_set):
    result = run_cli("--live-db", str(live), "--backup-dir", str(backup_set), "--restore-uploads-root", "/app/uploads",
                     "--policy", "normal")
    assert result.returncode == 0
    # argparse rejects an unknown choice before the gate runs
    bad = run_cli("--live-db", str(live), "--backup-dir", str(backup_set), "--restore-uploads-root", "/app/uploads",
                  "--policy", "bogus")
    assert bad.returncode != 0


# ------------------------------------------------------------------------ configuration
@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-1", "abc", "1e999"])
def test_invalid_freshness_values_are_config_errors(live, backup_set, value):
    result = run_cli("--live-db", str(live), "--backup-dir", str(backup_set), "--restore-uploads-root", "/app/uploads",
                     f"--max-age-hours={value}", "--json")
    assert result.returncode == evidence_gate.EXIT_CONFIG, f"{value!r} was accepted"


def test_missing_live_database_is_reported(live, backup_set, tmp_path):
    result = run_cli("--live-db", str(tmp_path / "nope.db"),
                     "--backup-dir", str(backup_set), "--restore-uploads-root", "/app/uploads", "--json")
    assert result.returncode == evidence_gate.EXIT_MISSING


def test_missing_backup_set_is_reported(live, tmp_path):
    result = run_cli("--live-db", str(live), "--backup-dir", str(tmp_path / "no-such"), "--restore-uploads-root", "/app/uploads",
                     "--json")
    assert result.returncode == evidence_gate.EXIT_MISSING


def test_unverified_set_can_be_allowed(tmp_path, live):
    path = make_set(live, tmp_path / "backups" / "s1", verification="SKIPPED")
    refused = run_cli("--live-db", str(live), "--backup-dir", str(path), "--restore-uploads-root", "/app/uploads")
    assert refused.returncode == evidence_gate.EXIT_INVALID
    allowed = run_cli("--live-db", str(live), "--backup-dir", str(path), "--restore-uploads-root", "/app/uploads", "--allow-unverified")
    assert allowed.returncode == 0, allowed.stdout


def test_wrong_application_id_is_refused(live, backup_set):
    result = run_cli("--live-db", str(live), "--backup-dir", str(backup_set), "--restore-uploads-root", "/app/uploads",
                     "--application-id", "42", "--json")
    assert result.returncode == evidence_gate.EXIT_INVALID
    assert any("application_id" in reason for reason in json.loads(result.stdout)["errors"])


# ------------------------------------------------------------------------------- purity
def test_the_gate_does_not_modify_either_database(live, backup_set):
    before_live = live.read_bytes()
    before_backup = (backup_set / "orders.db").read_bytes()
    assert run_cli("--live-db", str(live), "--backup-dir", str(backup_set), "--restore-uploads-root", "/app/uploads").returncode == 0
    assert live.read_bytes() == before_live
    assert (backup_set / "orders.db").read_bytes() == before_backup


def test_a_refusal_leaves_no_side_effects(tmp_path, live, backup_set):
    changed = build_database(tmp_path / "changed.db", variant="wr_row")
    before = changed.read_bytes()
    run_cli("--live-db", str(changed), "--backup-dir", str(backup_set), "--restore-uploads-root", "/app/uploads")
    assert changed.read_bytes() == before
    assert sorted(p.name for p in backup_set.iterdir()) == sorted(
        [name for name in ("orders.db", "uploads.tar.gz", "backup_manifest.json",
                           "manifest.sha256")])
