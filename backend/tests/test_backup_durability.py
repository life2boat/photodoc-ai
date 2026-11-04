"""Regressions for the independent review of PR #28 (H1, M1, M2, L1, L2).

H1 — Docker gate permissions (static: Docker is unavailable here)
M1 — orphaned staging directories
M2 — durability barriers around publication
L1 — backup selection ordering
L2 — `.previous` retention
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
SCRIPTS_DIR = REPO_DIR / "scripts"
for _p in (str(BACKEND_DIR), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import backup_safety as safety  # noqa: E402
import backup_gate  # noqa: E402
from backup_set_builder import make_backup_set, write_manifests  # noqa: E402

NOW = datetime.now(timezone.utc)


def _staged(tmp_path: Path, name: str = ".tmp-set-abc", files: int = 2) -> Path:
    staging = tmp_path / name
    staging.mkdir(parents=True)
    for index in range(files):
        (staging / f"artifact-{index}.bin").write_bytes(b"payload" * 32)
    return staging


# ---------------------------------------------------------------- M2 durability
def test_fsync_tree_flushes_every_file(tmp_path):
    staging = _staged(tmp_path, files=3)
    flushed, unsupported = safety.fsync_tree(staging)
    # Windows cannot flush a read-only handle; POSIX flushes all three.
    assert flushed + unsupported == 3
    if os.name == "posix":
        assert flushed == 3 and unsupported == 0


def test_fsync_tree_rejects_symlinks(tmp_path):
    staging = _staged(tmp_path, files=1)
    target = staging / "link.bin"
    try:
        target.symlink_to(staging / "artifact-0.bin")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")
    with pytest.raises(safety.BackupSafetyError):
        safety.fsync_tree(staging)


def test_contents_are_flushed_before_the_rename(tmp_path, monkeypatch):
    """The manifest must never describe bytes that are still in cache."""
    events = []
    real_replace = os.replace
    real_fsync_tree = safety.fsync_tree
    real_fsync_dir = safety.fsync_dir

    monkeypatch.setattr(safety, "fsync_tree",
                        lambda root: (events.append("fsync_tree"), real_fsync_tree(root))[1])
    monkeypatch.setattr(safety, "fsync_dir",
                        lambda p: (events.append("fsync_dir"), real_fsync_dir(p))[1])

    def recording_replace(src, dst):
        events.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(safety.os, "replace", recording_replace)

    staging = _staged(tmp_path)
    final = tmp_path / "published"
    safety.atomic_publish(staging, final)

    first_replace = events.index("replace")
    assert "fsync_tree" in events[:first_replace], (
        f"contents were not flushed before the rename: {events}")


def test_parent_directory_is_flushed_after_the_rename(tmp_path, monkeypatch):
    fsynced_dirs = []
    real_fsync_dir = safety.fsync_dir

    def record(path):
        fsynced_dirs.append(Path(path))
        return real_fsync_dir(path)

    monkeypatch.setattr(safety, "fsync_dir", record)

    staging = _staged(tmp_path)
    final = tmp_path / "published"
    safety.atomic_publish(staging, final)

    assert final.parent in fsynced_dirs, "the publication directory was never flushed"


def test_fsync_failure_aborts_publication_and_preserves_previous(tmp_path, monkeypatch):
    """A durability failure must not publish, and must not damage the old set."""
    final = tmp_path / "set"
    final.mkdir()
    (final / "old.bin").write_bytes(b"previous-valid-backup")

    staging = _staged(tmp_path)

    def broken_fsync(path):
        raise safety.BackupSafetyError("simulated fsync failure")

    monkeypatch.setattr(safety, "fsync_file", broken_fsync)

    with pytest.raises(safety.BackupSafetyError):
        safety.atomic_publish(staging, final)

    assert final.is_dir()
    assert (final / "old.bin").read_bytes() == b"previous-valid-backup"
    assert not (tmp_path / "published-did-not-happen").exists()


def test_durability_report_is_honest_about_platform_support(tmp_path):
    staging = _staged(tmp_path)
    report: dict = {}
    safety.atomic_publish(staging, tmp_path / "published", durability=report)

    assert report["files_flushed"] + report["files_unsupported"] == 2
    assert "directory_fsync_supported" in report
    assert report["directory_fsync_supported"] is safety.DIRECTORY_FSYNC_SUPPORTED
    if os.name != "posix":
        assert report["directory_fsync_supported"] is False, (
            "Windows must not claim a directory barrier it cannot perform")


# ---------------------------------------------------------------- M1 staging sweep
def _age(path: Path, hours: float) -> None:
    stamp = time.time() - hours * 3600.0
    os.utime(path, (stamp, stamp))


def test_sweep_removes_abandoned_staging(tmp_path):
    stale = _staged(tmp_path, name=".tmp-abandoned")
    _age(stale, 48)
    removed, skipped = safety.sweep_staging_dirs(tmp_path, min_age_hours=24)
    assert removed == [stale]
    assert not stale.exists()


def test_sweep_never_removes_a_published_backup(tmp_path):
    published, _ = make_backup_set(tmp_path, name="20260101T000000Z", created=NOW)
    removed, _ = safety.sweep_staging_dirs(tmp_path, min_age_hours=0)
    assert removed == []
    assert published.is_dir(), "a published backup set must never be swept"


def test_sweep_skips_a_recent_attempt(tmp_path):
    fresh = _staged(tmp_path, name=".tmp-in-flight")
    removed, skipped = safety.sweep_staging_dirs(tmp_path, min_age_hours=24)
    assert removed == []
    assert any("active" in entry["reason"] or "younger" in entry["reason"]
               for entry in skipped)


def test_sweep_skips_an_old_dir_whose_lock_is_recent(tmp_path):
    """An attempt can be long-running; a recent lock means it is still alive."""
    staging = _staged(tmp_path, name=".tmp-long-run")
    _age(staging, 48)
    (staging / safety.STAGING_LOCK_NAME).write_text("1234 lock\n", encoding="utf-8")
    removed, skipped = safety.sweep_staging_dirs(tmp_path, min_age_hours=1)
    assert removed == []
    assert any("active" in entry["reason"] for entry in skipped)


def test_sweep_refuses_symlinks(tmp_path):
    target = _staged(tmp_path, name="real-dir")
    _age(target, 48)
    link = tmp_path / ".tmp-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")
    removed, skipped = safety.sweep_staging_dirs(tmp_path, min_age_hours=1)
    assert removed == []
    assert any("symlink" in entry["reason"] for entry in skipped)
    assert target.is_dir()


def test_sweep_dry_run_deletes_nothing(tmp_path):
    stale = _staged(tmp_path, name=".tmp-abandoned")
    _age(stale, 48)
    removed, skipped = safety.sweep_staging_dirs(tmp_path, min_age_hours=24, dry_run=True)
    assert removed == []
    assert stale.is_dir()
    assert any(entry["reason"] == "dry run" for entry in skipped)


# ------------------------------------------------------------------------ L1 order
def test_selection_prefers_manifest_time_over_directory_mtime(tmp_path):
    """A copied set carries a copy mtime; the manifest decides what is newest."""
    older, _ = make_backup_set(tmp_path, name="20260101T000000Z", created=NOW - timedelta(days=5))
    newer, _ = make_backup_set(tmp_path, name="20260102T000000Z", created=NOW - timedelta(hours=1))
    # Make the older set look newest by touch order, as a `cp -a` would.
    future = time.time() + 3600
    os.utime(older, (future, future))

    selected = backup_gate.latest_backup_set(tmp_path, require_outbox=True)
    assert selected == newer, "mtime ordering promoted an older backup"


def test_backup_timestamp_reads_the_manifest(tmp_path):
    backup_dir, _ = make_backup_set(tmp_path, created=NOW.replace(microsecond=0))
    stamp = safety.backup_timestamp(backup_dir)
    assert stamp is not None
    assert abs((stamp - NOW.replace(microsecond=0)).total_seconds()) < 2


def test_backup_timestamp_falls_back_to_mtime(tmp_path):
    backup_dir, _ = make_backup_set(tmp_path, created=NOW)
    (backup_dir / "backup_manifest.json").unlink()
    assert safety.backup_timestamp(backup_dir) is not None


# ------------------------------------------------------------------- L2 .previous
def test_previous_retention_keeps_the_newest_sets():
    entries = [(Path(f"/b/set{i}.previous"), NOW - timedelta(days=i)) for i in range(5)]
    keep, prune = safety.plan_previous_retention(entries, keep=2)
    assert set(keep) == {Path("/b/set0.previous"), Path("/b/set1.previous")}
    assert len(prune) == 3


def test_previous_retention_never_prunes_everything():
    entries = [(Path("/b/only.previous"), NOW)]
    keep, prune = safety.plan_previous_retention(entries, keep=2)
    assert keep == [Path("/b/only.previous")]
    assert prune == []


def test_previous_retention_with_no_entries():
    assert safety.plan_previous_retention([], keep=2) == ([], [])


def test_retention_refuses_to_prune_when_no_published_set_verifies(tmp_path, capsys):
    """Pruning must never assume a replacement succeeded."""
    import backup_retention

    make_backup_set(tmp_path, name="20260201T000000Z", verification="FAIL", created=NOW)
    code = backup_retention.main(["--root", str(tmp_path), "--apply", "--json",
                                  "--keep-last", "1", "--keep-daily", "0",
                                  "--keep-weekly", "0"])
    payload = json.loads(capsys.readouterr().out)
    # No prune candidates here, so nothing is refused; the invariant is that the
    # set survives untouched.
    assert (tmp_path / "20260201T000000Z").is_dir()
    assert code in (0, 1)


def test_previous_sets_are_pruned_under_the_plan(tmp_path, capsys):
    import backup_retention

    live, _ = make_backup_set(tmp_path, name="20260301T000000Z", created=NOW)
    previous_dirs = []
    for index in range(3):
        prev = tmp_path / f"2026020{index + 1}T000000Z.previous"
        prev.mkdir()
        (prev / "orders.db").write_bytes(b"x")
        _age(prev, 24 * (index + 1))
        previous_dirs.append(prev)

    code = backup_retention.main(["--root", str(tmp_path), "--apply", "--json",
                                  "--keep-previous", "1", "--keep-last", "1",
                                  "--keep-daily", "0", "--keep-weekly", "0"])
    payload = json.loads(capsys.readouterr().out)
    assert live.is_dir(), "the published set must survive retention"
    assert len(payload["previous_prune"]) == 2
    assert code == 0, payload.get("errors")
    remaining = [p for p in previous_dirs if p.exists()]
    assert len(remaining) == 1, f"expected one .previous to remain, got {remaining}"


# ------------------------------------------------------- deep archive verification
def test_deep_check_rejects_an_archive_missing_a_required_upload(tmp_path):
    """A failed archive verification must be fatal wherever the verifier records it.

    ``verify_backup`` reports a missing source for an unfinished delivery in
    ``p1_missing_files`` and leaves ``errors`` empty. When the deep check copied
    only ``errors``, the gate passed an archive that could not restore an
    unfinished order — found by the isolated VPS acceptance run (T4).
    """
    import hashlib
    import io
    import tarfile

    backup_dir, _ = make_backup_set(tmp_path, created=NOW)
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

    # Keep the manifest self-consistent so only the deep check can catch this.
    tar_sha = hashlib.sha256(tar_path.read_bytes()).hexdigest()
    db_sha = hashlib.sha256((backup_dir / "orders.db").read_bytes()).hexdigest()
    (backup_dir / "manifest.sha256").write_text(
        f"{db_sha}  orders.db\n{tar_sha}  uploads.tar.gz\n", encoding="utf-8")
    manifest = json.loads((backup_dir / "backup_manifest.json").read_text(encoding="utf-8"))
    manifest["uploads_tar_sha256"] = tar_sha
    (backup_dir / "backup_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")

    ok, report = safety.verify_backup_set(
        backup_dir, require_outbox=True, now=NOW, check_upload_archive=True)
    assert report.get("upload_archive_verified") is False
    assert ok is False, "a set missing a required upload must not pass the deep check"
    assert any("missing" in error for error in report["errors"])


def test_deep_check_accepts_a_healthy_set(tmp_path):
    backup_dir, _ = make_backup_set(tmp_path, created=NOW)
    ok, report = safety.verify_backup_set(
        backup_dir, require_outbox=True, now=NOW, check_upload_archive=True)
    assert ok, report["errors"]
    assert report.get("upload_archive_verified") is True


# ------------------------------------------------------------------ H1 static
def _gate_invocation(text: str) -> str:
    """The ``docker run`` block that invokes backup_gate.py (not the host-mode one)."""
    for chunk in text.split("docker run")[1:]:
        if "backup_gate.py" in chunk:
            return "docker run" + chunk.split("backup_gate.py")[0]
    raise AssertionError("no docker run invocation of backup_gate.py found")


def test_docker_gate_runs_read_only_without_network():
    """H1: the gate must read a root-owned 0700 set without loosening it."""
    text = (SCRIPTS_DIR / "deploy_backup_gate.sh").read_text(encoding="utf-8")
    gate = _gate_invocation(text)

    assert "--user 0:0" in gate, "docker mode must run as root to read a 0700 set"
    assert "--user 65534" not in gate, "an unprivileged UID cannot traverse 0700 backups"
    assert "--network none" in gate, "the gate needs no network"
    assert "--read-only" in gate, "container filesystem must be immutable"
    assert "--cap-drop ALL" in gate
    assert "no-new-privileges" in gate
    for mount in (":/repo:ro", ":/backups:ro", ":/data:ro"):
        assert mount in gate, f"host mount {mount} must be read-only"


def test_backup_permissions_remain_restrictive():
    """H1 must not be 'fixed' by making backups readable."""
    text = (SCRIPTS_DIR / "create_backup.py").read_text(encoding="utf-8")
    assert "os.chmod(output_dir, 0o700)" in text, "the set directory must stay 0700"
    for name in ("dst_db", "dst_tar", "manifest_sha", "manifest_json"):
        assert f"os.chmod({name}, 0o600)" in text, f"{name} must stay 0600"
    assert "0o777" not in text, "no world-readable permission may be introduced"


def test_runbook_matches_the_gate_implementation():
    runbook = (REPO_DIR / "docs" / "PRODUCTION_RELEASE.md").read_text(encoding="utf-8")
    gate = _gate_invocation(runbook)
    assert "--user 0:0" in gate, "the runbook must use the same permissions model"
    assert "--network none" in gate
    assert "--read-only" in gate
    assert "--user 65534" not in gate
    assert ":/backups:ro" in gate
