"""Linux-safe rollback bundle creation and verification tests."""

from __future__ import annotations

import io
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from build_rollback_bundle import build_bundle, build_bundle_from_zip  # noqa: E402
from rollback_bundle_common import (  # noqa: E402
    MANIFEST_NAME,
    render_manifest,
    require_secret_free_content,
    sha256_bytes,
)
from verify_rollback_bundle import rehearse_extract, verify_bundle  # noqa: E402


REQUIRED_RELEASE_FILES = [
    "docker-compose.yml",
    "backend/main.py",
    "frontend/nginx.conf",
]


def _synthetic_release(root: Path) -> None:
    files = {
        "docker-compose.yml": b"services: {}\n",
        "backend/main.py": b"print('backend')\n",
        "backend/Dockerfile": b"FROM scratch\n",
        "frontend/nginx.conf": b"events {}\n",
    }
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def test_bundle_build_verify_and_empty_directory_restore(tmp_path):
    source = tmp_path / "release"
    source.mkdir()
    _synthetic_release(source)
    bundle = tmp_path / "known-good.tar.gz"
    external_manifest = tmp_path / "manifest.sha256"

    result = build_bundle(source, bundle, external_manifest)
    assert result["file_count"] == 4
    assert len(result["bundle_sha256"]) == 64

    contents = verify_bundle(bundle, required=REQUIRED_RELEASE_FILES)
    restore = tmp_path / "restore"
    rehearse_extract(contents, restore)

    for relative in REQUIRED_RELEASE_FILES:
        assert (restore / relative).is_file()
    assert (restore / "manifest.sha256").read_bytes() == external_manifest.read_bytes()
    assert all("\\" not in name for name in contents)


@pytest.mark.parametrize(
    "forbidden_path",
    [
        "backend/.env",
        ".env",
        "backend/uploads/customer.jpg",
        "orders.db",
        "backups/orders.db.bak",
        "logs/backend.log",
        "frontend/dev-server.pid",
        "private_key.pem",
        "api_token.txt",
    ],
)
def test_builder_rejects_secrets_and_mutable_data(tmp_path, forbidden_path):
    source = tmp_path / "release"
    source.mkdir()
    _synthetic_release(source)
    path = source / forbidden_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"must not ship")

    with pytest.raises(ValueError, match="Forbidden"):
        build_bundle(source, tmp_path / "bundle.tar.gz", tmp_path / "manifest.sha256")


@pytest.mark.parametrize(
    "unsafe_name",
    [r"backend\main.py", "../escape.txt", "/absolute.txt", "C:/absolute.txt"],
)
def test_verifier_rejects_unsafe_archive_paths(tmp_path, unsafe_name):
    bundle = tmp_path / "unsafe.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        data = b"unsafe"
        info = tarfile.TarInfo(unsafe_name)
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))

    with pytest.raises(ValueError):
        verify_bundle(bundle)


def test_restore_requires_empty_directory(tmp_path):
    source = tmp_path / "release"
    source.mkdir()
    _synthetic_release(source)
    bundle = tmp_path / "bundle.tar.gz"
    build_bundle(source, bundle, tmp_path / "manifest.sha256")
    contents = verify_bundle(bundle)
    restore = tmp_path / "restore"
    restore.mkdir()
    (restore / "existing.txt").write_text("occupied", encoding="utf-8")

    with pytest.raises(ValueError, match="must be empty"):
        rehearse_extract(contents, restore)


def test_audited_zip_normalization_omits_only_runtime_state(tmp_path):
    source = tmp_path / "release"
    source.mkdir()
    _synthetic_release(source)
    source_zip = tmp_path / "legacy.zip"
    manifest = tmp_path / "production.sha256"
    manifest.write_bytes(
        render_manifest(
            (path.relative_to(source).as_posix(), sha256_bytes(path.read_bytes()))
            for path in source.rglob("*")
            if path.is_file()
        )
    )
    with zipfile.ZipFile(source_zip, "w") as archive:
        for path in source.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())
        archive.writestr("frontend/dev-server.pid", "123")

    bundle = tmp_path / "normalized.tar.gz"
    result = build_bundle_from_zip(
        source_zip, manifest, bundle, tmp_path / "manifest.sha256"
    )
    assert result["omitted_mutable_files"] == 1
    contents = verify_bundle(bundle, required=REQUIRED_RELEASE_FILES)
    assert "frontend/dev-server.pid" not in contents


def test_zip_normalization_refuses_secret_extra(tmp_path):
    source = tmp_path / "release"
    source.mkdir()
    _synthetic_release(source)
    source_zip = tmp_path / "legacy.zip"
    manifest = tmp_path / "production.sha256"
    manifest.write_bytes(
        render_manifest(
            (path.relative_to(source).as_posix(), sha256_bytes(path.read_bytes()))
            for path in source.rglob("*")
            if path.is_file()
        )
    )
    with zipfile.ZipFile(source_zip, "w") as archive:
        for path in source.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())
        archive.writestr("backend/.env", "SECRET=value")

    with pytest.raises(ValueError, match="Secret-bearing"):
        build_bundle_from_zip(
            source_zip,
            manifest,
            tmp_path / "bundle.tar.gz",
            tmp_path / "manifest.sha256",
        )


@pytest.mark.parametrize(
    "payload",
    [
        b"-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n",
        b"SUPABASE_SERVICE_KEY=sb_secret_embeddedcredential\n",
        b"SMTP_PASSWORD=actual-production-style-password\n",
        b"API_TOKEN: 'actual-production-style-token'\n",
        b"PASSWORD=actual-production-style-password\n",
        b"TOKEN=actual-production-style-token\n",
    ],
)
def test_builder_rejects_embedded_secret_material(tmp_path, payload):
    source = tmp_path / "release"
    source.mkdir()
    _synthetic_release(source)
    (source / "config.txt").write_bytes(payload)

    with pytest.raises(ValueError, match="Forbidden content-secret categories"):
        build_bundle(source, tmp_path / "bundle.tar.gz", tmp_path / "manifest.sha256")


def test_content_scan_accepts_explicit_placeholders():
    require_secret_free_content(
        "example.env",
        b"SMTP_PASSWORD=<production-app-password>\n"
        b"YANDEX_DISK_TOKEN=${YANDEX_DISK_TOKEN}\n"
        b"ROBOKASSA_PASSWORD1=ci_pass1\n",
    )


def test_content_scan_error_never_echoes_secret_value():
    secret_value = "do-not-print-this-credential"
    with pytest.raises(ValueError) as exc_info:
        require_secret_free_content(
            "config.env", f"SMTP_PASSWORD={secret_value}\n".encode("utf-8")
        )
    message = str(exc_info.value)
    assert secret_value not in message
    assert "config.env" in message
    assert "credential-assignment" in message


def test_verifier_rejects_embedded_secret_with_valid_manifest(tmp_path):
    name = "backend/config.txt"
    data = b"API_TOKEN=embedded-production-style-token\n"
    manifest = render_manifest([(name, sha256_bytes(data))])
    bundle = tmp_path / "secret-bearing.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        for member_name, member_data in ((name, data), (MANIFEST_NAME, manifest)):
            info = tarfile.TarInfo(member_name)
            info.size = len(member_data)
            archive.addfile(info, io.BytesIO(member_data))

    with pytest.raises(ValueError, match="Forbidden content-secret categories"):
        verify_bundle(bundle)
