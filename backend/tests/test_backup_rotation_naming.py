"""Regression tests for H-03: real archive names and a hard maximum age."""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_rotation  # noqa: E402

NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


def make(path: Path, days_old: float):
    path.mkdir(parents=True)
    (path / "orders.db").write_text("x")
    stamp = (NOW - timedelta(days=days_old)).timestamp()
    os.utime(path, (stamp, stamp))
    os.utime(path / "orders.db", (stamp, stamp))


def test_timestamp_directories_from_run_backup_are_recognised(tmp_path):
    """The real archive name produced by scripts/run_backup.sh must be visible."""
    make(tmp_path / "20260101T000000Z", 5)
    found = backup_rotation.list_backups(tmp_path)
    assert [p.name for p in found] == ["20260101T000000Z"]


def test_malformed_and_foreign_names_are_ignored(tmp_path):
    make(tmp_path / "20260101T000000Z", 1)
    for name in ("20260101T00000", "2026-01-01", "notes", "backup-old"):
        (tmp_path / name).mkdir()
    names = {p.name for p in backup_rotation.list_backups(tmp_path)}
    assert names == {"20260101T000000Z"}


def test_hard_max_age_overrides_keep_min(tmp_path):
    make(tmp_path / "20250101T000000Z", 400)
    make(tmp_path / "20250102T000000Z", 390)

    result = backup_rotation.rotate(tmp_path, now=NOW, retention_days=3, keep_min=5, max_age_days=30)

    remaining = {p.name for p in tmp_path.iterdir() if p.is_dir()}
    assert remaining == set(), "keep_min kept personal-data archives past the hard limit"
    assert len(result["removed"]) == 2


def test_keep_min_protects_within_the_window(tmp_path):
    make(tmp_path / "20261108T000000Z", 0.5)
    make(tmp_path / "20261107T000000Z", 1.5)
    make(tmp_path / "20261106T000000Z", 2.5)

    result = backup_rotation.rotate(tmp_path, now=NOW, retention_days=1, keep_min=2, max_age_days=30)

    remaining = {p.name for p in tmp_path.iterdir() if p.is_dir()}
    assert remaining == {"20261108T000000Z", "20261107T000000Z"}
    assert len(result["removed"]) == 1


def test_dry_run_with_hard_limit_changes_nothing(tmp_path):
    make(tmp_path / "20250101T000000Z", 400)
    result = backup_rotation.rotate(tmp_path, now=NOW, keep_min=5, max_age_days=30, dry_run=True)
    assert result["removed"]
    assert (tmp_path / "20250101T000000Z").exists()
