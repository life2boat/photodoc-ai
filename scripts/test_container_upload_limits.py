"""Smoke test containerized Nginx reverse proxy upload limits and contracts."""
from __future__ import annotations

import argparse
import io
import sys
import urllib.request
import uuid

JPEG_MAGIC = b"\xff\xd8\xff" + b"\x00" * 10


def encode_multipart_formdata(fields: dict[str, str], files: list[tuple[str, str, bytes, str]]) -> tuple[bytes, str]:
    boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(f"{value}\r\n".encode("utf-8"))
    for field_name, filename, data, content_type in files:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode("utf-8"))
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        body.extend(data)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    content_type = f"multipart/form-data; boundary={boundary}"
    return bytes(body), content_type


def post_order(base_url: str, fields: dict[str, str], files: list[tuple[str, str, bytes, str]]) -> tuple[int, str]:
    url = f"{base_url.rstrip('/')}/api/order"
    body, content_type = encode_multipart_formdata(fields, files)
    req = urllib.request.Request(url, data=body, headers={"Content-Type": content_type}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", errors="replace")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8080", help="Base URL of frontend Nginx")
    args = parser.parse_args()
    base_url = args.url

    print(f"Testing containerized Nginx upload limits at {base_url}...")

    # 1. Small file upload
    print("1. Testing small file upload (1 KB)...")
    status, text = post_order(base_url, {"name": "CI Client", "phone": "79991112233", "format": "10x15"},
                              [("files", "small.jpg", JPEG_MAGIC + b"x" * 1024, "image/jpeg")])
    assert status == 200, f"Expected 200, got {status}: {text}"
    print("   PASS (200 OK)")

    # 2. Duplicate file names
    print("2. Testing duplicate file names in same order...")
    status, text = post_order(base_url, {"name": "CI Client", "phone": "79991112233", "format": "10x15"},
                              [("files", "photo.jpg", JPEG_MAGIC + b"file1", "image/jpeg"),
                               ("files", "photo.jpg", JPEG_MAGIC + b"file2", "image/jpeg")])
    assert status == 200, f"Expected 200, got {status}: {text}"
    print("   PASS (200 OK)")

    # 3. Order over 50 MiB, within 80 MiB limit (3 x 18 MiB = 54 MiB) through Nginx
    print("3. Testing order > 50 MiB, within 80 MiB limit (54 MiB through containerized Nginx)...")
    one_mb = b"\x00" * (1024 * 1024)
    file_18mb = JPEG_MAGIC + one_mb * 18
    status, text = post_order(base_url, {"name": "Large Client", "phone": "79991112233", "format": "10x15"},
                              [("files", "f1.jpg", file_18mb, "image/jpeg"),
                               ("files", "f2.jpg", file_18mb, "image/jpeg"),
                               ("files", "f3.jpg", file_18mb, "image/jpeg")])
    assert status == 200, f"Expected 200 for 54 MiB order through Nginx, got {status}: {text}"
    print("   PASS (200 OK — Nginx 100M limit successfully handled 54 MiB payload)")

    # 4. Single file exceeding 20 MiB limit (21 MiB)
    print("4. Testing single file exceeding 20 MiB limit (21 MiB)...")
    file_21mb = JPEG_MAGIC + one_mb * 21
    status, text = post_order(base_url, {"name": "Over File", "phone": "79991112233", "format": "10x15"},
                              [("files", "big.jpg", file_21mb, "image/jpeg")])
    assert status == 413, f"Expected 413, got {status}: {text}"
    print("   PASS (413 Payload Too Large)")

    # 5. Order exceeding 80 MiB limit (5 x 17 MiB = 85 MiB)
    print("5. Testing order exceeding 80 MiB limit (85 MiB)...")
    file_17mb = JPEG_MAGIC + one_mb * 17
    status, text = post_order(base_url, {"name": "Over Order", "phone": "79991112233", "format": "10x15"},
                              [("files", f"f{i}.jpg", file_17mb, "image/jpeg") for i in range(5)])
    assert status == 413, f"Expected 413, got {status}: {text}"
    print("   PASS (413 Payload Too Large)")

    # 6. Exceeding 10 files limit (11 files)
    print("6. Testing order exceeding 10 files limit (11 files)...")
    status, text = post_order(base_url, {"name": "Eleven Files", "phone": "79991112233", "format": "10x15"},
                              [("files", f"p{i}.jpg", JPEG_MAGIC + b"data", "image/jpeg") for i in range(11)])
    assert status == 400, f"Expected 400, got {status}: {text}"
    print("   PASS (400 Bad Request)")

    # 7. Invalid image magic signature
    print("7. Testing invalid image magic signature...")
    status, text = post_order(base_url, {"name": "Fake Magic", "phone": "79991112233", "format": "10x15"},
                              [("files", "fake.jpg", b"NOT_A_VALID_JPEG_HEADER", "image/jpeg")])
    assert status == 400, f"Expected 400, got {status}: {text}"
    print("   PASS (400 Bad Request)")

    print("All containerized Nginx upload limit contracts PASSED!")


if __name__ == "__main__":
    main()
