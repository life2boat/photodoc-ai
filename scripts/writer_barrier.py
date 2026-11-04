#!/usr/bin/env python3
"""PhotoDoc AI — continuous writer-exclusion barrier.

The backup gate can prove that a *snapshot* was consistent and that no writer was
mid-transaction at that instant. It cannot prove that nothing writes afterwards:
a SQLite read transaction does not exclude future writers, and this process exits
long before the migration runs.

This tool establishes the part that can be established mechanically, and refuses
when it cannot:

* no running container is attached to the database volume (optional ``--volume``);
* no host process holds the database file (or its ``-wal``/``-journal``/``-shm``
  siblings) open — checked through ``/proc/<pid>/fd`` on Linux;
* an ``BEGIN EXCLUSIVE`` transaction can be acquired, proving no other connection
  currently holds a lock.

It is deliberately not a proof of the whole system's silence. The trusted boundary
is documented in ``docs/BACKUP_AND_RESTORE.md`` §3.1: the VPS host itself, the
enumerated writer set (application container, delivery worker, scheduled jobs,
operator-run scripts), and the documented rule that nothing is started between the
barrier and the completed cutover. Anything outside that boundary — an external
process with its own filesystem access, a second host sharing the volume — is out
of scope and must be prevented by infrastructure, not by this check.

Exit codes::

    0  no writer detected and an exclusive lock was acquired
    1  a writer (or an unprovable state) was detected — the deployment must stop
    3  this platform cannot inspect processes (use --allow-unsupported only in tests)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from backup_safety import readonly_uri  # noqa: E402

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_UNSUPPORTED = 3

#: Sibling files SQLite may hold open alongside the database.
SIBLINGS = ("-wal", "-journal", "-shm")


def _candidate_paths(db: Path) -> list[Path]:
    resolved = db.resolve()
    return [resolved] + [Path(str(resolved) + suffix) for suffix in SIBLINGS]


def holders(db: Path, *, proc_root: Path = Path("/proc")) -> tuple[list[dict], str | None]:
    """Processes holding the database open. ``(holders, unsupported_reason)``."""
    targets = set()
    for candidate in _candidate_paths(db):
        try:
            targets.add((candidate.stat().st_dev, candidate.stat().st_ino))
        except OSError:
            # the sibling may legitimately not exist
            continue
    if not targets:
        return [], f"cannot stat the database file: {db}"
    if not proc_root.is_dir():
        return [], "process inspection is unavailable (/proc is missing)"

    found: list[dict] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        fd_dir = entry / "fd"
        try:
            descriptors = list(fd_dir.iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                target = descriptor.stat()
            except OSError:
                continue
            if (target.st_dev, target.st_ino) in targets:
                try:
                    link = os.readlink(descriptor)
                except OSError:
                    link = "?"
                found.append({"pid": pid, "fd": descriptor.name, "path": link})
                break
    return found, None


def exclusive_lock_available(db: Path, *, timeout: float = 2.0) -> tuple[bool, str | None]:
    """Try to take ``BEGIN EXCLUSIVE``; release immediately."""
    try:
        conn = sqlite3.connect(readonly_uri(db).replace("mode=ro", "mode=rw"),
                               uri=True, timeout=timeout)
    except sqlite3.Error as exc:
        return False, f"cannot open the database for a lock probe: {exc}"
    try:
        conn.execute("BEGIN EXCLUSIVE")
        conn.rollback()
        return True, None
    except sqlite3.Error as exc:
        return False, f"cannot acquire an exclusive lock: {exc}"
    finally:
        conn.close()


def containers_using_volume(volume: str) -> tuple[list[str], str | None]:
    if not volume:
        return [], None
    if shutil.which("docker") is None:
        return [], "docker is not available; the container check cannot run"
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"volume={volume}", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"docker ps failed: {exc}"
    if result.returncode != 0:
        return [], f"docker ps failed (exit {result.returncode}): {result.stderr.strip()[:200]}"
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return names, None


def fingerprint(db: Path) -> str:
    """``device:inode:size:mtime_ns`` — changes when anything writes to the file.

    Nanosecond mtime on purpose: a whole-second timestamp cannot distinguish a
    write that lands in the same second as the gate, which is exactly the window
    this check exists to close.
    """
    try:
        st = Path(db).stat()
    except OSError:
        return "unknown"
    mtime_ns = getattr(st, "st_mtime_ns", None)
    if mtime_ns is None:  # pragma: no cover - very old Python/filesystem
        mtime_ns = int(st.st_mtime * 1_000_000_000)
    return f"{st.st_dev}:{st.st_ino}:{st.st_size}:{mtime_ns}"


def evaluate(db: Path, *, volume: str | None, allow_unsupported: bool,
             expect_fingerprint: str | None = None) -> tuple[int, dict]:
    report: dict = {
        "db": str(db),
        "volume": volume or None,
        "fingerprint": fingerprint(db),
        "expected_fingerprint": expect_fingerprint,
        "ok": False,
        "blocking": [],
        "warnings": [],
        "holders": [],
        "exclusive_lock": None,
        "containers": [],
        "unsupported": False,
        "trusted_boundary": (
            "host + enumerated writers (application container, delivery worker, scheduled jobs, "
            "operator scripts); nothing may be started between this barrier and the completed "
            "cutover"),
    }

    if not Path(db).exists():
        report["blocking"].append(f"database file does not exist: {db}")
        report["reason"] = "cannot-prove-absence"
        return EXIT_BLOCKED, report

    if expect_fingerprint is not None and report["fingerprint"] != expect_fingerprint:
        # Something wrote between the gate that approved the backup and this check.
        report["blocking"].append(
            f"the database changed after it was approved (approved={expect_fingerprint}, "
            f"now={report['fingerprint']})")
        report["reason"] = "database-changed"
        return EXIT_BLOCKED, report

    vol_holders, vol_error = containers_using_volume(volume or "")
    if vol_error:
        report["blocking"].append(vol_error)
    report["containers"] = vol_holders
    if vol_holders:
        report["blocking"].append(
            f"containers are attached to the volume: {', '.join(vol_holders)}")

    proc_holders, unsupported = holders(Path(db))
    report["holders"] = proc_holders
    if unsupported:
        report["unsupported"] = True
        if allow_unsupported:
            # Tests only: the platform cannot enumerate holders, so exclusion is
            # recorded as unproven rather than silently assumed.
            report["warnings"] = [f"process inspection unavailable ({unsupported}); "
                                  "explicitly permitted for this run"]
        else:
            report["blocking"].append(
                f"cannot prove writer absence: {unsupported}. Refusing without --allow-unsupported.")
            report["reason"] = "unsupported-platform"
            return EXIT_UNSUPPORTED, report
    if proc_holders:
        report["blocking"].append(
            "host processes hold the database open: "
            + ", ".join(f"pid {h['pid']}" for h in proc_holders))

    locked, lock_error = exclusive_lock_available(Path(db))
    report["exclusive_lock"] = locked
    if not locked:
        report["blocking"].append(lock_error or "exclusive lock unavailable")

    report["ok"] = not report["blocking"]
    return (EXIT_OK if report["ok"] else EXIT_BLOCKED), report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PhotoDoc AI — writer exclusion barrier")
    parser.add_argument("--db", type=Path, required=True,
                        help="host path of the database that will be migrated")
    parser.add_argument("--volume", help="docker volume backing the database (optional)")
    parser.add_argument("--expect-fingerprint", default=None,
                        help="refuse if the database changed since this fingerprint was taken")
    parser.add_argument("--allow-unsupported", action="store_true",
                        help="permit platforms without /proc (tests only)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    code, report = evaluate(args.db, volume=args.volume, allow_unsupported=args.allow_unsupported,
                            expect_fingerprint=args.expect_fingerprint)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print("==================================================")
        print("     PhotoDoc AI — Writer Exclusion Barrier       ")
        print("==================================================")
        print(f"Database:        {report['db']}")
        print(f"Volume:          {report['volume']}")
        print(f"Containers:      {report['containers'] or 'none attached'}")
        print(f"Host holders:    {len(report['holders'])}")
        print(f"Exclusive lock:  {report['exclusive_lock']}")
        print(f"Fingerprint:     {report['fingerprint']}")
        if report["ok"]:
            print("BARRIER: PASS — no writer detected.")
        else:
            print("BARRIER: BLOCKED")
            for item in report["blocking"]:
                print(f"  - {item}")
        print("==================================================")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
