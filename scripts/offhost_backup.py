#!/usr/bin/env python3
"""PhotoDoc AI — off-host backup replication (S6-02).

Takes the newest *verified* local backup set, seals it into a single encrypted
archive, pushes it off the VPS and prunes old remote copies under a retention
plan that can never delete the newest one.

Design constraints this file is written against:

* **Not operational by default.** ``--dry-run`` is the default mode; without
  ``--execute`` nothing is encrypted, pushed or pruned. Without a complete
  configuration the script refuses and says exactly what is missing, which is the
  intended state until the owner supplies storage and credentials.
* **No plaintext leaves the host.** The archive is encrypted before the push
  command is invoked; the push only ever sees ``*.tar.gz.gpg``.
* **No secret in the repository.** The passphrase lives in a file whose mode is
  checked to be 0600, referenced by path only.
* **A restore must not need the VPS.** Every remote artifact is self-contained
  (database + uploads + manifests) plus a sidecar checksum, so recovery is
  "download, decrypt, verify, restore".

Exit codes: 0 success/dry-run-ok, 2 configuration incomplete, 3 local backup
unusable, 4 encryption failed, 5 push failed, 6 verification failed, 7 prune refused.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from backup_safety import (  # noqa: E402
    plan_retention,
    sha256_file,
    verify_backup_set,
)

REQUIRED_CONFIG = ("BACKUP_ROOT", "OFFHOST_DEST", "OFFHOST_PUSH_CMD")
ENCRYPTION_CONFIG = ("OFFHOST_PASSFILE", "OFFHOST_RECIPIENT")

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_LOCAL = 3
EXIT_ENCRYPT = 4
EXIT_PUSH = 5
EXIT_VERIFY = 6
EXIT_PRUNE = 7


@dataclass
class Config:
    values: Dict[str, str] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self.values.get(key, default)

    def get_int(self, key: str, default: int) -> int:
        try:
            return int(self.values.get(key, default))
        except (TypeError, ValueError):
            return default

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def encryption_mode(self) -> str:
        if self.values.get("OFFHOST_RECIPIENT"):
            return "public-key"
        if self.values.get("OFFHOST_PASSFILE"):
            return "passphrase"
        return "none"


def read_env_file(path: Path) -> Dict[str, str]:
    """Parse a ``KEY=VALUE`` env file without executing it."""
    values: Dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def load_config(env: Optional[Dict[str, str]] = None, env_file: Optional[Path] = None) -> Config:
    values: Dict[str, str] = {}
    if env_file is not None and Path(env_file).is_file():
        values.update(read_env_file(Path(env_file)))
    values.update({k: v for k, v in os.environ.items() if k.startswith("OFFHOST_") or k == "BACKUP_ROOT"})
    if env:
        values.update(env)

    missing = [key for key in REQUIRED_CONFIG if not values.get(key)]
    if not any(values.get(key) for key in ENCRYPTION_CONFIG):
        missing.append("OFFHOST_PASSFILE or OFFHOST_RECIPIENT")
    return Config(values=values, missing=sorted(missing))


def validate_passfile(path: str) -> Optional[str]:
    """Return an error string, or None when the passphrase file is usable.

    The permission check is POSIX-only on purpose: the deployment target is Linux,
    where a group/world-readable key genuinely leaks the passphrase, while on
    Windows the permission bits are not meaningful and a hard failure there would
    only make the component untestable off-target.
    """
    candidate = Path(path)
    if not candidate.is_file():
        return f"passphrase file not found: {candidate}"
    try:
        size = candidate.stat().st_size
    except OSError as exc:
        return f"passphrase file is unreadable: {exc}"
    if size == 0:
        return "passphrase file is empty"
    if os.name != "nt":
        mode = candidate.stat().st_mode & 0o777
        if mode & 0o077:
            return f"passphrase file is too permissive ({oct(mode)}); expected 0600"
    return None


def build_encrypt_command(cfg: Config, source: Path, target: Path) -> List[str]:
    """Construct the gpg invocation. Kept pure so it can be asserted in tests."""
    base = [
        "gpg", "--batch", "--yes", "--quiet",
        "--cipher-algo", "AES256",
        "--s2k-digest-algo", "SHA512",
        "--output", str(target),
    ]
    if cfg.get("OFFHOST_RECIPIENT"):
        return base + ["--encrypt", "--trust-model", "always",
                       "--recipient", str(cfg.get("OFFHOST_RECIPIENT")), str(source)]
    return base + ["--symmetric", "--passphrase-file", str(cfg.get("OFFHOST_PASSFILE")), str(source)]


def select_local_backup(root: Path, *, require_outbox: bool) -> Optional[Path]:
    """Newest complete, verified backup set under ``root``."""
    root = Path(root)
    if not root.is_dir():
        return None
    candidates: List[Path] = []
    for entry in root.iterdir():
        if not entry.is_dir() or entry.is_symlink() or entry.name.startswith("."):
            continue
        ok, _ = verify_backup_set(entry, require_outbox=require_outbox)
        if ok:
            candidates.append(entry)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def make_archive(backup_dir: Path, target_tar: Path) -> None:
    """Deterministic, link-free tar of one backup set."""
    with tarfile.open(target_tar, "w:gz") as tar:
        for path in sorted(Path(backup_dir).rglob("*")):
            arcname = Path(backup_dir.name) / path.relative_to(backup_dir)
            info = tar.gettarinfo(str(path), arcname=str(arcname))
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if path.is_file():
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
            else:
                tar.addfile(info)


RunCommand = Callable[..., subprocess.CompletedProcess]


def run_command(cmd: Sequence[str], *, log: List[str]) -> subprocess.CompletedProcess:
    printable = " ".join(shlex.quote(part) for part in cmd)
    log.append(f"exec: {printable}")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=3600, check=False)


def render(cmd_template: str, **fields: str) -> List[str]:
    """Render a command template, quoting each substituted value."""
    rendered = cmd_template
    for key, value in fields.items():
        rendered = rendered.replace("{" + key + "}", shlex.quote(str(value)))
    return shlex.split(rendered)


def prune_remote(
    cfg: Config,
    *,
    remote_listing: Sequence[tuple[Path, datetime]],
    keep_last: int,
    keep_daily: int,
    keep_weekly: int,
    protected: Optional[Path] = None,
) -> tuple[List[Path], List[Path]]:
    """Plan a remote prune, never removing ``protected`` or the newest artifact."""
    keep, prune = plan_retention(
        remote_listing, keep_last=keep_last, keep_daily=keep_daily, keep_weekly=keep_weekly)
    if protected is not None and protected in prune:
        prune = [p for p in prune if p != protected]
        keep.append(protected)
    return keep, prune


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="PhotoDoc AI — off-host backup replication")
    parser.add_argument("--env-file", type=Path, help="configuration file (KEY=VALUE)")
    parser.add_argument("--require-outbox", action="store_true",
                        help="require the paid-email outbox table in the local set")
    parser.add_argument("--execute", action="store_true",
                        help="actually encrypt, push and prune (default is a dry run)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/photodoc-backup"),
                        help="where the last-run status is written")
    args = parser.parse_args(argv)

    log: List[str] = []
    report: Dict[str, Any] = {
        "mode": "execute" if args.execute else "dry-run",
        "ok": False,
        "artifact": None,
        "artifact_sha256": None,
        "pushed": False,
        "pruned": [],
        "errors": [],
        "log": log,
    }

    cfg = load_config(env_file=args.env_file)
    report["encryption_mode"] = cfg.encryption_mode
    if not cfg.complete:
        report["errors"].append(
            "configuration incomplete — off-host replication is not active. Missing: "
            + ", ".join(cfg.missing))
        report["missing"] = cfg.missing
        _emit(report, args.json)
        return EXIT_CONFIG

    if cfg.encryption_mode == "passphrase":
        problem = validate_passfile(str(cfg.get("OFFHOST_PASSFILE")))
        if problem:
            report["errors"].append(problem)
            _emit(report, args.json)
            return EXIT_CONFIG

    if shutil.which("gpg") is None:
        report["errors"].append("gpg is not installed on this host")
        _emit(report, args.json)
        return EXIT_CONFIG

    local = select_local_backup(Path(str(cfg.get("BACKUP_ROOT"))),
                                require_outbox=args.require_outbox)
    if local is None:
        report["errors"].append(
            f"no complete verified backup set under {cfg.get('BACKUP_ROOT')}; refusing to "
            "replicate an unverified backup")
        _emit(report, args.json)
        return EXIT_LOCAL
    report["local_backup"] = str(local)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact_name = f"photodoc-{local.name}-{stamp}.tar.gz.gpg"

    if not args.execute:
        report["ok"] = True
        report["planned_artifact"] = artifact_name
        report["planned_dest"] = cfg.get("OFFHOST_DEST")
        _emit(report, args.json)
        return EXIT_OK

    workdir = Path(tempfile.mkdtemp(prefix="photodoc-offhost-"))
    try:
        plain_tar = workdir / f"{local.name}.tar.gz"
        make_archive(local, plain_tar)
        artifact = workdir / artifact_name
        encrypt = build_encrypt_command(cfg, plain_tar, artifact)
        result = run_command(encrypt, log=log)
        if result.returncode != 0:
            report["errors"].append(f"encryption failed (exit {result.returncode}): {getattr(result, 'stderr', '')[:400]}")
            _emit(report, args.json)
            return EXIT_ENCRYPT
        if not artifact.is_file() or artifact.stat().st_size == 0:
            report["errors"].append("encryption produced no output artifact")
            _emit(report, args.json)
            return EXIT_ENCRYPT

        digest = sha256_file(artifact)
        checksum_file = workdir / f"{artifact_name}.sha256"
        checksum_file.write_text(f"{digest}  {artifact_name}\n", encoding="utf-8")
        report["artifact"] = artifact_name
        report["artifact_sha256"] = digest

        push = render(str(cfg.get("OFFHOST_PUSH_CMD")),
                      artifact=str(artifact), checksum=str(checksum_file),
                      dest=str(cfg.get("OFFHOST_DEST")), name=artifact_name)
        pushed = run_command(push, log=log)
        if pushed.returncode != 0:
            report["errors"].append(
                f"push failed (exit {pushed.returncode}): {getattr(pushed, 'stderr', '')[:400]}")
            _emit(report, args.json)
            return EXIT_PUSH
        report["pushed"] = True

        # Verification is only claimed when a fetch-back command exists; a push
        # that exited 0 is a claim, not evidence.
        verify_template = cfg.get("OFFHOST_VERIFY_CMD")
        if verify_template:
            check = render(verify_template, name=artifact_name,
                           dest=str(cfg.get("OFFHOST_DEST")), sha256=digest)
            verified = run_command(check, log=log)
            if verified.returncode != 0:
                report["errors"].append("remote verification failed; artifact not confirmed")
                _emit(report, args.json)
                return EXIT_VERIFY
            report["remote_verified"] = True
        else:
            report["remote_verified"] = False
            report["errors"].append(
                "no OFFHOST_VERIFY_CMD configured: push is unverified and must not be "
                "called operational")

        report["ok"] = report["pushed"] and report["remote_verified"]
        _write_state(args.state_dir, report)
        _emit(report, args.json)
        return EXIT_OK if report["ok"] else EXIT_VERIFY
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _write_state(state_dir: Path, report: Dict[str, Any]) -> None:
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        payload = dict(report)
        payload.pop("log", None)
        (state_dir / "last-run.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8")
    except OSError:
        pass


def _emit(report: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2, default=str))
        return
    print("==================================================")
    print("   PhotoDoc AI — Off-host Backup Replication      ")
    print("==================================================")
    print(f"Mode:            {report['mode']}")
    print(f"Encryption:      {report.get('encryption_mode')}")
    print(f"Local backup:    {report.get('local_backup')}")
    if report.get("planned_artifact"):
        print(f"Would create:    {report['planned_artifact']} -> {report.get('planned_dest')}")
    if report.get("artifact"):
        print(f"Artifact:        {report['artifact']} sha256={report['artifact_sha256']}")
    print(f"Pushed:          {report.get('pushed')}")
    print(f"Remote verified: {report.get('remote_verified')}")
    for entry in report.get("log", []):
        print(f"  {entry}")
    if report["errors"]:
        print("--------------------------------------------------")
        for error in report["errors"]:
            print(f"  ! {error}")
    print("==================================================")


if __name__ == "__main__":
    raise SystemExit(main())
