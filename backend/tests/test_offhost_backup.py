"""Off-host backup: configuration safety, dry-run default, and refusal behaviour.

S6-02 is *not* operational — there is no storage and no credentials. What must be
true today is that the component is inert, honest about what it is missing, and
cannot silently upload plaintext or delete the last good copy.
"""

from __future__ import annotations

import os
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

import offhost_backup as offhost  # noqa: E402
from backup_set_builder import make_backup_set  # noqa: E402

NOW = datetime.now(timezone.utc)


# ------------------------------------------------------------------- config parsing
def test_read_env_file_handles_comments_exports_and_quotes(tmp_path):
    path = tmp_path / "backup.env"
    path.write_text(
        "# comment\n"
        "\n"
        "BACKUP_ROOT=/var/backups/photodoc\n"
        "export OFFHOST_DEST='/srv/offsite'\n"
        'OFFHOST_PUSH_CMD="scp -q {artifact} {dest}/"\n'
        "NOT_A_PAIR\n",
        encoding="utf-8")
    values = offhost.read_env_file(path)
    assert values["BACKUP_ROOT"] == "/var/backups/photodoc"
    assert values["OFFHOST_DEST"] == "/srv/offsite"
    assert values["OFFHOST_PUSH_CMD"] == "scp -q {artifact} {dest}/"
    assert "NOT_A_PAIR" not in values


def test_config_reports_every_missing_requirement():
    cfg = offhost.load_config(env={})
    assert cfg.complete is False
    assert "BACKUP_ROOT" in cfg.missing
    assert "OFFHOST_DEST" in cfg.missing
    assert "OFFHOST_PUSH_CMD" in cfg.missing
    assert any("PASSFILE" in entry for entry in cfg.missing)


def test_complete_config_with_passphrase():
    cfg = offhost.load_config(env={
        "BACKUP_ROOT": "/var/backups/photodoc",
        "OFFHOST_DEST": "/srv/offsite",
        "OFFHOST_PUSH_CMD": "scp {artifact} {dest}/",
        "OFFHOST_PASSFILE": "/etc/photodoc/backup.passphrase",
    })
    assert cfg.complete is True
    assert cfg.encryption_mode == "passphrase"


def test_complete_config_with_recipient():
    cfg = offhost.load_config(env={
        "BACKUP_ROOT": "/b", "OFFHOST_DEST": "/d", "OFFHOST_PUSH_CMD": "cp",
        "OFFHOST_RECIPIENT": "backup@example.org",
    })
    assert cfg.complete is True
    assert cfg.encryption_mode == "public-key"


def test_passfile_validation(tmp_path):
    missing = offhost.validate_passfile(str(tmp_path / "nope"))
    assert missing and "not found" in missing

    empty = tmp_path / "empty"
    empty.write_text("", encoding="utf-8")
    assert "empty" in offhost.validate_passfile(str(empty))


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not meaningful on Windows")
def test_passfile_must_not_be_world_readable(tmp_path):
    path = tmp_path / "pass"
    path.write_text("synthetic-passphrase", encoding="utf-8")
    path.chmod(0o644)
    problem = offhost.validate_passfile(str(path))
    assert problem and "too permissive" in problem

    path.chmod(0o600)
    assert offhost.validate_passfile(str(path)) is None


# ------------------------------------------------------------------ encrypt command
def test_encrypt_command_uses_passphrase_file(tmp_path):
    cfg = offhost.load_config(env={
        "BACKUP_ROOT": "/b", "OFFHOST_DEST": "/d", "OFFHOST_PUSH_CMD": "cp",
        "OFFHOST_PASSFILE": "/etc/photodoc/backup.passphrase",
    })
    cmd = offhost.build_encrypt_command(cfg, Path("/tmp/in.tar.gz"), Path("/tmp/out.gpg"))
    assert cmd[0] == "gpg"
    assert "--symmetric" in cmd
    assert "--passphrase-file" in cmd
    assert "/etc/photodoc/backup.passphrase" in cmd
    assert Path(cmd[-1]) == Path("/tmp/in.tar.gz")
    assert Path(cmd[cmd.index("--output") + 1]) == Path("/tmp/out.gpg")


def test_encrypt_command_uses_recipient(tmp_path):
    cfg = offhost.load_config(env={
        "BACKUP_ROOT": "/b", "OFFHOST_DEST": "/d", "OFFHOST_PUSH_CMD": "cp",
        "OFFHOST_RECIPIENT": "backup@example.org",
    })
    cmd = offhost.build_encrypt_command(cfg, Path("/tmp/in"), Path("/tmp/out"))
    assert "--encrypt" in cmd
    assert "--recipient" in cmd
    assert "backup@example.org" in cmd
    assert "--symmetric" not in cmd


def test_render_quotes_substitutions():
    cmd = offhost.render("scp {artifact} {dest}/", artifact="/tmp/a b.gpg", dest="/srv/x")
    assert cmd[0] == "scp"
    assert "/tmp/a b.gpg" in cmd


# --------------------------------------------------------------------- selection
def test_selects_the_newest_verified_local_set(tmp_path):
    make_backup_set(tmp_path, name="20260101T000000Z", created=NOW - timedelta(days=3))
    newest, _ = make_backup_set(tmp_path, name="20260102T000000Z", created=NOW - timedelta(hours=1))

    selected = offhost.select_local_backup(tmp_path, require_outbox=True)
    assert selected == newest


def test_never_selects_an_unverified_set(tmp_path):
    make_backup_set(tmp_path, name="20260101T000000Z", verification="FAIL", created=NOW)
    assert offhost.select_local_backup(tmp_path, require_outbox=True) is None


def test_never_selects_a_set_without_the_outbox_when_required(tmp_path):
    make_backup_set(tmp_path, name="20260101T000000Z", with_outbox=False, created=NOW)
    assert offhost.select_local_backup(tmp_path, require_outbox=True) is None
    assert offhost.select_local_backup(tmp_path, require_outbox=False) is not None


# ------------------------------------------------------------------ prune safety
def test_prune_never_removes_the_newest_or_protected(tmp_path):
    cfg = offhost.load_config(env={
        "BACKUP_ROOT": "/b", "OFFHOST_DEST": "/d", "OFFHOST_PUSH_CMD": "cp",
        "OFFHOST_PASSFILE": "/p",
    })
    listing = [(Path(f"/remote/{i}"), NOW - timedelta(days=i)) for i in range(10)]
    keep, prune = offhost.prune_remote(
        cfg, remote_listing=listing, keep_last=2, keep_daily=0, keep_weekly=0,
        protected=Path("/remote/9"))
    assert Path("/remote/0") in keep, "the newest remote copy must never be pruned"
    assert Path("/remote/9") not in prune, "the just-uploaded artifact must be protected"


# ------------------------------------------------------------------------ main()
def test_main_refuses_without_configuration(capsys):
    for key in list(os.environ):
        if key.startswith("OFFHOST_") or key == "BACKUP_ROOT":
            os.environ.pop(key, None)
    code = offhost.main(["--json"])
    assert code == offhost.EXIT_CONFIG
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["missing"]


def _complete_env(tmp_path, backup_root):
    passfile = tmp_path / "pass"
    if not passfile.exists():
        passfile.write_text("synthetic-passphrase", encoding="utf-8")
        # The component enforces 0600 on POSIX; a file created with the default
        # umask is 0644 and is correctly rejected. Set the mode the deployment
        # procedure requires.
        passfile.chmod(0o600)
    return {
        "BACKUP_ROOT": str(backup_root),
        "OFFHOST_DEST": "/srv/offsite",
        "OFFHOST_PUSH_CMD": "true {artifact} {checksum} {dest}/{name}",
        "OFFHOST_PASSFILE": str(passfile),
    }


def test_dry_run_does_not_execute_anything(tmp_path, monkeypatch, capsys):
    make_backup_set(tmp_path / "backups", created=NOW)
    env = _complete_env(tmp_path, tmp_path / "backups")
    monkeypatch.setattr(offhost.shutil, "which", lambda name: "/usr/bin/gpg")
    executed = []
    monkeypatch.setattr(offhost, "run_command", lambda *a, **k: executed.append(a) or None)

    code = offhost.main(["--env-file", str(_write_env(tmp_path, env)), "--json"])
    assert code == offhost.EXIT_OK
    assert executed == [], "a dry run must not shell out at all"
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert payload["pushed"] is False


def _write_env(tmp_path, env):
    path = tmp_path / "backup.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
    return path


def test_requires_gpg(tmp_path, monkeypatch, capsys):
    make_backup_set(tmp_path / "backups", created=NOW)
    env = _complete_env(tmp_path, tmp_path / "backups")
    monkeypatch.setattr(offhost.shutil, "which", lambda name: None)
    code = offhost.main(["--env-file", str(_write_env(tmp_path, env)), "--execute", "--json"])
    assert code == offhost.EXIT_CONFIG
    assert any("gpg" in e for e in __import__("json").loads(capsys.readouterr().out)["errors"])


def test_fails_closed_when_no_verified_local_backup(tmp_path, monkeypatch, capsys):
    (tmp_path / "backups").mkdir()
    env = _complete_env(tmp_path, tmp_path / "backups")
    monkeypatch.setattr(offhost.shutil, "which", lambda name: "/usr/bin/gpg")
    code = offhost.main(["--env-file", str(_write_env(tmp_path, env)), "--execute", "--json"])
    assert code == offhost.EXIT_LOCAL


class _Result:
    def __init__(self, returncode, stderr=""):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


def _fake_runner(tmp_path, *, push_rc=0, verify_rc=0, verify_configured=True):
    """Simulate gpg creating the artifact, then the push and verify commands."""
    calls = []

    def runner(cmd, *, log):
        calls.append(cmd)
        log.append("fake: " + " ".join(cmd))
        if cmd and cmd[0] == "gpg":
            out = Path(cmd[cmd.index("--output") + 1])
            out.write_bytes(b"ciphertext-not-plaintext")
            return _Result(0)
        if verify_configured and len(cmd) >= 2 and "verify" in " ".join(cmd):
            return _Result(verify_rc)
        return _Result(push_rc)

    return runner, calls


def test_execute_encrypts_then_pushes_and_never_sends_plaintext(tmp_path, monkeypatch, capsys):
    make_backup_set(tmp_path / "backups", created=NOW)
    env = _complete_env(tmp_path, tmp_path / "backups")
    env["OFFHOST_VERIFY_CMD"] = "verify {name} {sha256}"
    monkeypatch.setattr(offhost.shutil, "which", lambda name: "/usr/bin/gpg")
    runner, calls = _fake_runner(tmp_path)
    monkeypatch.setattr(offhost, "run_command", runner)

    code = offhost.main(["--env-file", str(_write_env(tmp_path, env)),
                         "--execute", "--require-outbox", "--json"])
    payload = __import__("json").loads(capsys.readouterr().out)

    assert code == offhost.EXIT_OK, payload
    assert payload["pushed"] is True
    assert payload["remote_verified"] is True
    assert payload["artifact"].endswith(".tar.gz.gpg"), "only ciphertext may be pushed"
    # gpg must be invoked before any push command.
    assert calls[0][0] == "gpg"
    pushed_args = " ".join(" ".join(c) for c in calls[1:])
    assert ".tar.gz.gpg" in pushed_args
    assert "<photodoc" not in pushed_args


def test_push_failure_is_reported(tmp_path, monkeypatch, capsys):
    make_backup_set(tmp_path / "backups", created=NOW)
    env = _complete_env(tmp_path, tmp_path / "backups")
    monkeypatch.setattr(offhost.shutil, "which", lambda name: "/usr/bin/gpg")
    runner, _ = _fake_runner(tmp_path, push_rc=1)
    monkeypatch.setattr(offhost, "run_command", runner)

    code = offhost.main(["--env-file", str(_write_env(tmp_path, env)), "--execute", "--json"])
    assert code == offhost.EXIT_PUSH


def test_unverified_push_is_never_called_operational(tmp_path, monkeypatch, capsys):
    make_backup_set(tmp_path / "backups", created=NOW)
    env = _complete_env(tmp_path, tmp_path / "backups")
    monkeypatch.setattr(offhost.shutil, "which", lambda name: "/usr/bin/gpg")
    runner, _ = _fake_runner(tmp_path)
    monkeypatch.setattr(offhost, "run_command", runner)

    code = offhost.main(["--env-file", str(_write_env(tmp_path, env)),
                         "--execute", "--require-outbox", "--json"])
    payload = __import__("json").loads(capsys.readouterr().out)
    # No OFFHOST_VERIFY_CMD in the env file -> pushed but explicitly not operational.
    assert payload["remote_verified"] is False
    assert payload["ok"] is False
    assert code == offhost.EXIT_VERIFY
    assert any("unverified" in e for e in payload["errors"])


def test_failed_remote_verification_is_reported(tmp_path, monkeypatch, capsys):
    make_backup_set(tmp_path / "backups", created=NOW)
    env = _complete_env(tmp_path, tmp_path / "backups")
    env["OFFHOST_VERIFY_CMD"] = "verify {name}"
    monkeypatch.setattr(offhost.shutil, "which", lambda name: "/usr/bin/gpg")
    runner, _ = _fake_runner(tmp_path, verify_rc=1)
    monkeypatch.setattr(offhost, "run_command", runner)

    code = offhost.main(["--env-file", str(_write_env(tmp_path, env)), "--execute", "--json"])
    assert code == offhost.EXIT_VERIFY
