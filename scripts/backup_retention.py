#!/usr/bin/env python3
"""PhotoDoc AI — local backup retention.

Prunes old local backup sets under a retention plan. Dry-run by default; the
newest verified set is never removed, and a set is never removed when it is the
only one that verifies.

Usage::

    python scripts/backup_retention.py --root /var/backups/photodoc          # plan only
    python scripts/backup_retention.py --root /var/backups/photodoc --apply  # delete
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from backup_safety import (  # noqa: E402
    PREVIOUS_SUFFIX,
    plan_previous_retention,
    plan_retention,
    sweep_staging_dirs,
    verify_backup_set,
)


def collect_previous_sets(root: Path) -> List[tuple[Path, datetime]]:
    """``.previous`` sets: both ``<name>.previous`` and ``<name>.previous-<stamp>``."""
    entries: List[tuple[Path, datetime]] = []
    for entry in Path(root).iterdir() if Path(root).is_dir() else []:
        if not entry.is_dir() or entry.is_symlink():
            continue
        if entry.name.startswith(".") or PREVIOUS_SUFFIX not in entry.name:
            continue
        entries.append((entry, _created_at(entry)))
    return entries


def _created_at(path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return datetime.now(timezone.utc)


def collect_sets(root: Path) -> List[tuple[Path, datetime]]:
    entries: List[tuple[Path, datetime]] = []
    for entry in Path(root).iterdir() if Path(root).is_dir() else []:
        if not entry.is_dir() or entry.is_symlink():
            continue
        if entry.name.startswith(".") or entry.name.endswith(PREVIOUS_SUFFIX):
            continue
        entries.append((entry, _created_at(entry)))
    return entries


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PhotoDoc AI — local backup retention")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--keep-last", type=int, default=7)
    parser.add_argument("--keep-daily", type=int, default=14)
    parser.add_argument("--keep-weekly", type=int, default=8)
    parser.add_argument("--require-outbox", action="store_true")
    parser.add_argument("--keep-previous", type=int, default=2,
                        help="number of .previous sets to retain (default 2)")
    parser.add_argument("--sweep-staging", action="store_true",
                        help="also remove abandoned .tmp-* staging directories")
    parser.add_argument("--staging-min-age-hours", type=float, default=24.0,
                        help="minimum age before a staging directory may be swept")
    parser.add_argument("--apply", action="store_true", help="actually delete (default: plan only)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    entries = collect_sets(args.root)
    keep, prune = plan_retention(
        entries, keep_last=args.keep_last, keep_daily=args.keep_daily,
        keep_weekly=args.keep_weekly)
    previous_entries = collect_previous_sets(args.root)
    previous_keep, previous_prune = plan_previous_retention(
        previous_entries, keep=args.keep_previous)

    report: Dict[str, Any] = {
        "root": str(args.root),
        "mode": "apply" if args.apply else "plan",
        "keep": [str(p) for p in keep],
        "prune": [str(p) for p in prune],
        "previous_keep": [str(p) for p in previous_keep],
        "previous_prune": [str(p) for p in previous_prune],
        "deleted": [],
        "staging_removed": [],
        "staging_skipped": [],
        "refused": [],
        "errors": [],
    }

    # A published set must still verify before anything is pruned: pruning must
    # never run on the assumption that a replacement succeeded.
    verified = [p for p, _ in entries if verify_backup_set(
        p, require_outbox=args.require_outbox, require_verified=False)[0]]
    if (args.apply and (prune or previous_prune)) and not verified:
        report["refused"] = [str(p) for p in list(prune) + list(previous_prune)]
        report["errors"].append(
            "refusing to prune: no published backup set currently verifies")
        _emit(report, args.json)
        return 1

    if args.apply and prune:
        survivors = [p for p in verified if p not in prune]
        if verified and not survivors:
            report["refused"] = [str(p) for p in prune]
            report["errors"].append(
                "refusing to prune: every verified backup set would be deleted")
            _emit(report, args.json)
            return 1

        for path in prune:
            try:
                shutil.rmtree(path)
                report["deleted"].append(str(path))
            except OSError as exc:
                report["errors"].append(f"failed to delete {path}: {exc}")

    if args.apply and previous_prune:
        # `.previous` is a fallback, never the primary copy: refuse if no verified
        # published set would remain as the thing being fallen back *from*.
        for path in previous_prune:
            try:
                shutil.rmtree(path)
                report["deleted"].append(str(path))
            except OSError as exc:
                report["errors"].append(f"failed to delete {path}: {exc}")

    if args.sweep_staging:
        removed, skipped = sweep_staging_dirs(
            args.root, min_age_hours=args.staging_min_age_hours, dry_run=not args.apply)
        report["staging_removed"] = [str(p) for p in removed]
        report["staging_skipped"] = skipped

    _emit(report, args.json)
    return 0 if not report["errors"] else 1


def _emit(report: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2))
        return
    print(f"Retention ({report['mode']}) under {report['root']}")
    print(f"  keep:  {len(report['keep'])}")
    for path in report["keep"]:
        print(f"    + {path}")
    print(f"  prune: {len(report['prune'])}")
    for path in report["prune"]:
        print(f"    - {path}")
    if report.get("previous_keep"):
        print(f"  .previous keep: {len(report['previous_keep'])}")
    if report.get("previous_prune"):
        print(f"  .previous prune: {len(report['previous_prune'])}")
        for path in report["previous_prune"]:
            print(f"    - {path}")
    if report.get("staging_removed"):
        print(f"  staging swept: {len(report['staging_removed'])}")
    for path in report["deleted"]:
        print(f"    deleted {path}")
    for error in report["errors"]:
        print(f"  ! {error}")


if __name__ == "__main__":
    raise SystemExit(main())
