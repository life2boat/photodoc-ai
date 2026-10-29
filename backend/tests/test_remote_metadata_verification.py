"""
PhotoDoc AI — R3.1 Remote Metadata Verification & Security Tests.

Tests:
1. Matching remote metadata permits delivery confirmation.
2. Wrong remote hash blocks delivery confirmation.
3. Wrong remote size blocks delivery confirmation.
4. Missing or malformed remote metadata never produces a false PASS.
5. Failed verification always preserves the local source.
6. Integration-style test exercising actual _remote_verified() logic with synthetic HTTP responses (no real OAuth/API calls).
7. Path traversal and symlink rejection before upload.
8. Cross-order path substitution protection.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import main
from delivery import enqueue, get_uploads_dir, perform_job_cleanup, set_uploads_dir, utc_now


class SyntheticResponse:
    def __init__(self, status_code: int = 200, data: dict | None = None, content: bytes = b""):
        self.status_code = status_code
        self._data = data or {}
        self.content = content

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"Synthetic HTTP {self.status_code}")


def test_matching_remote_metadata_permits_delivery_confirmation(tmp_path, monkeypatch):
    """1. Matching remote metadata (exact SHA-256 and exact size) confirms delivery."""
    source = tmp_path / "1_good_photo.jpg"
    source.write_bytes(b"exact-matching-bytes-12345")
    expected_sha = hashlib.sha256(b"exact-matching-bytes-12345").hexdigest()
    expected_size = len(b"exact-matching-bytes-12345")

    fake_metadata = {
        "type": "file",
        "sha256": expected_sha,
        "size": expected_size,
    }

    with patch.object(main.httpx, "get", return_value=SyntheticResponse(200, fake_metadata)):
        # Real _remote_verified logic executed against synthetic HTTP response
        verified = main._remote_verified("fake-token", "remote/path.jpg", expected_sha, expected_size)
        assert verified is True


def test_wrong_remote_hash_blocks_delivery_confirmation(tmp_path, monkeypatch):
    """2. Wrong remote hash raises RuntimeError and blocks delivery."""
    expected_sha = hashlib.sha256(b"original-content").hexdigest()
    expected_size = 16

    fake_metadata = {
        "type": "file",
        "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
        "size": expected_size,
    }

    with patch.object(main.httpx, "get", return_value=SyntheticResponse(200, fake_metadata)):
        with pytest.raises(RuntimeError, match="does not match"):
            main._remote_verified("fake-token", "remote/path.jpg", expected_sha, expected_size)


def test_wrong_remote_size_blocks_delivery_confirmation(tmp_path, monkeypatch):
    """3. Wrong remote size raises RuntimeError and blocks delivery."""
    expected_sha = hashlib.sha256(b"original-content").hexdigest()
    expected_size = 16

    fake_metadata = {
        "type": "file",
        "sha256": expected_sha,
        "size": 9999,  # Size mismatch!
    }

    with patch.object(main.httpx, "get", return_value=SyntheticResponse(200, fake_metadata)):
        with pytest.raises(RuntimeError, match="does not match"):
            main._remote_verified("fake-token", "remote/path.jpg", expected_sha, expected_size)


@pytest.mark.parametrize("bad_metadata", [
    {},  # Empty metadata
    {"type": "dir", "sha256": "abc", "size": 10},  # Directory instead of file
    {"type": "file", "sha256": "", "size": 10},  # Missing sha256
    {"type": "file", "sha256": None, "size": 10},  # None sha256
    {"type": "file", "sha256": "abc", "size": None},  # None size
    {"type": "file", "sha256": "abc", "size": "not-an-int"},  # Non-integer size
])
def test_malformed_remote_metadata_never_passes(bad_metadata):
    """4. Missing or malformed remote metadata never produces a false PASS."""
    with patch.object(main.httpx, "get", return_value=SyntheticResponse(200, bad_metadata)):
        with pytest.raises(RuntimeError):
            main._remote_verified("fake-token", "remote/path.jpg", "abc", 10)


def test_failed_verification_always_preserves_local_source(tmp_path, monkeypatch):
    """5. Failed remote verification preserves local source file completely."""
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    set_uploads_dir(uploads)

    source = uploads / "1_important.jpg"
    content = b"customer-precious-photo"
    source.write_bytes(content)

    monkeypatch.setenv("YANDEX_DISK_TOKEN", "fake-token")

    # Simulate remote returning wrong hash during upload confirmation
    def fake_get(url, **kwargs):
        if url.endswith("/upload"):
            return SyntheticResponse(200, {"href": "https://fake-upload.example.com/put"})
        return SyntheticResponse(200, {"type": "file", "sha256": "corrupted-hash", "size": len(content)})

    def fake_put(url, **kwargs):
        return SyntheticResponse(201)

    with patch.object(main.httpx, "get", side_effect=fake_get), \
         patch.object(main.httpx, "put", side_effect=fake_put):
        with pytest.raises(RuntimeError, match="does not match"):
            main.upload_to_yandex_disk(
                str(source),
                "RemoteFolder",
                "1_important.jpg",
                expected_sha256=hashlib.sha256(content).hexdigest(),
                expected_size=len(content),
                uploads_dir=uploads,
            )

    # Local source must remain completely intact!
    assert source.is_file()
    assert source.read_bytes() == content


def test_full_upload_and_verification_pipeline_with_synthetic_http(tmp_path, monkeypatch):
    """
    6. Integration-style test exercising actual _remote_verified()
    and upload_to_yandex_disk() using synthetic HTTP responses.
    """
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    set_uploads_dir(uploads)

    source = uploads / "1_real_flow.jpg"
    content = b"synthetic-jpeg-payload-for-yandex"
    source.write_bytes(content)
    real_sha = hashlib.sha256(content).hexdigest()
    real_size = len(content)

    monkeypatch.setenv("YANDEX_DISK_TOKEN", "mock-token-xyz")

    state = {"uploaded": False, "bytes_sent": b""}

    def synthetic_get(url, **kwargs):
        if url.endswith("/upload"):
            return SyntheticResponse(200, {"href": "https://storage.yandex.invalid/put-endpoint"})
        if url.endswith("/resources"):
            # Check resource endpoint
            if not state["uploaded"]:
                return SyntheticResponse(404)
            # After upload, return matching metadata
            return SyntheticResponse(200, {"type": "file", "sha256": real_sha, "size": real_size})
        raise ValueError(f"Unexpected GET {url}")

    def synthetic_put(url, **kwargs):
        if "put-endpoint" in url:
            # Collect uploaded chunks
            state["bytes_sent"] = b"".join(kwargs["content"])
            state["uploaded"] = True
            return SyntheticResponse(201)
        # ensure_folder put
        return SyntheticResponse(201)

    with patch.object(main.httpx, "get", side_effect=synthetic_get), \
         patch.object(main.httpx, "put", side_effect=synthetic_put):
        success = main.upload_to_yandex_disk(
            str(source),
            "PhotoDoc_Orders/Order_1",
            "1_real_flow.jpg",
            expected_sha256=real_sha,
            expected_size=real_size,
            uploads_dir=uploads,
        )
        assert success is True
        assert state["bytes_sent"] == content
        assert source.is_file(), "Local source preserved until delivery is confirmed and cleanup worker runs"


def test_symlink_upload_rejected(tmp_path, monkeypatch):
    """7. Reject symlinked source file before uploading."""
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    set_uploads_dir(uploads)

    real_file = uploads / "1_real.jpg"
    real_file.write_bytes(b"content")

    symlink_file = uploads / "1_symlink.jpg"
    try:
        symlink_file.symlink_to(real_file)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks not permitted in this environment")

    monkeypatch.setenv("YANDEX_DISK_TOKEN", "fake-token")

    with pytest.raises(RuntimeError, match="Symlinked source file rejected"):
        main.upload_to_yandex_disk(str(symlink_file), "Folder", "1_symlink.jpg", uploads_dir=uploads)


def test_path_traversal_upload_rejected(tmp_path, monkeypatch):
    """8. Reject path traversal before uploading."""
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    set_uploads_dir(uploads)

    monkeypatch.setenv("YANDEX_DISK_TOKEN", "fake-token")

    with pytest.raises(RuntimeError, match="Path traversal detected"):
        main.upload_to_yandex_disk(str(uploads / ".." / "secret.txt"), "Folder", "secret.txt", uploads_dir=uploads)


def test_cross_order_file_manipulation_rejected(tmp_path, monkeypatch):
    """9. A delivery job for Order 2 cannot upload or delete Order 1's file."""
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    set_uploads_dir(uploads)

    order1_file = uploads / "1_photo.jpg"
    order1_file.write_bytes(b"order-1-exclusive-photo")

    # Delivery job for Order 2 fraudulently pointing to Order 1's file
    malicious_job = {
        "id": "bad-job-2",
        "order_id": 2,  # Order 2!
        "kind": "file",
        "local_path": str(order1_file),  # File starts with 1_!
        "remote_path": "Orders/2/photo.jpg",
        "content_sha256": hashlib.sha256(b"order-1-exclusive-photo").hexdigest(),
        "content_size": len(b"order-1-exclusive-photo"),
    }

    with patch.object(main, "get_order_by_id", return_value={"id": 2, "name": "Eve"}):
        with pytest.raises(RuntimeError, match="does not match order ID"):
            main.deliver_job(malicious_job)
