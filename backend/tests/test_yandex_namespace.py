"""Native namespace handling against synthetic HTTP only; no OAuth or VPS access."""
import hashlib
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
import delivery
from yandex_paths import join_path, normalize_path

API = "https://cloud-api.yandex.net/v1/disk/resources"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Unexpected real HTTP transport")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.delenv("YANDEX_DISK_ROOT_DIR", raising=False)
    monkeypatch.setenv("YANDEX_DISK_TOKEN", "synthetic-namespace-token")
    monkeypatch.setenv("DELIVERY_WORKER_ENABLED", "0")
    monkeypatch.setenv("PHOTODOC_STAGING", "0")
    monkeypatch.setenv("PHOTODOC_ALLOW_FRESH_DB", "0")


def install(monkeypatch, handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(main.httpx, "get", client.get)
    monkeypatch.setattr(main.httpx, "put", client.put)
    return client


class Disk:
    def __init__(self, directories=()):
        self.dirs = set(directories)
        self.files = {}
        self.calls = []
        self.upload_attempts = 0
        self.committed_uploads = 0
        self.interrupt = False
        self.partial = b""

    def __call__(self, request):
        path = request.url.params.get("path")
        self.calls.append((request.method, request.url.path, path))
        if request.url.host == "upload.example.invalid":
            assert request.method == "PUT" and "authorization" not in request.headers
            self.upload_attempts += 1
            content = request.read()
            if self.interrupt:
                self.interrupt = False
                self.partial = content[:len(content) // 2]
                raise httpx.WriteError("synthetic-private-error")
            self.files[self.target] = content
            self.committed_uploads += 1
            return httpx.Response(201)
        assert request.method in ("GET", "PUT")
        if request.url.path.endswith("/upload"):
            assert request.method == "GET" and request.url.params["overwrite"] == "false"
            self.target = path
            return httpx.Response(200, json={"href": "https://upload.example.invalid/file"})
        if request.method == "GET":
            if path in self.dirs:
                return httpx.Response(200, json={"type": "dir"})
            if path in self.files:
                content = self.files[path]
                return httpx.Response(200, json={"type": "file", "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest()})
            return httpx.Response(404)
        # Simulate protected existing application ancestors that reject mkdir.
        # A native read-first implementation must never call this branch for them.
        if path in self.dirs:
            return httpx.Response(400)
        assert path not in ("app:", "app:/", "disk:", "disk:/")
        self.dirs.add(path)
        return httpx.Response(201)


@pytest.mark.parametrize("folder,existing,created", [
    ("app:/Stage/Order", {"app:/", "app:/Stage"}, ["app:/Stage/Order"]),
    ("disk:/Stage/Order", {"disk:/", "disk:/Stage"}, ["disk:/Stage/Order"]),
    ("Приложения/PhotoDoc/Stage/Order", {"Приложения", "Приложения/PhotoDoc"},
     ["Приложения/PhotoDoc/Stage", "Приложения/PhotoDoc/Stage/Order"]),
    ("Orders/Заказ_1/Фото 10x15", set(), ["Orders", "Orders/Заказ_1", "Orders/Заказ_1/Фото 10x15"]),
])
def test_existing_ancestors_are_read_only_and_nested_creation_is_idempotent(monkeypatch, folder, existing, created):
    disk = Disk(existing)
    with install(monkeypatch, disk):
        main.ensure_yandex_folder("synthetic", folder)
        main.ensure_yandex_folder("synthetic", folder)
    assert [p for method, _, p in disk.calls if method == "PUT"] == created
    assert not disk.files


@pytest.mark.parametrize("root", ["app:/", "disk:/"])
def test_namespace_root_is_lookup_only(monkeypatch, root):
    monkeypatch.setenv("YANDEX_DISK_ROOT_DIR", root)
    disk = Disk({root})
    with install(monkeypatch, disk):
        assert main.get_yandex_disk_root() == root
        main.ensure_yandex_folder("synthetic", root)
    assert disk.calls == [("GET", "/v1/disk/resources", root)]
    assert join_path(root, "file.jpg") == root + "file.jpg"


def test_missing_namespace_root_is_never_created(monkeypatch):
    disk = Disk()
    with install(monkeypatch, disk), pytest.raises(RuntimeError, match="namespace root"):
        main.ensure_yandex_folder("synthetic", "app:/Stage/Order")
    assert all(method == "GET" for method, _, _ in disk.calls)


def test_concurrent_workers_reconcile_409_without_duplicate_creation(monkeypatch):
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    exists = False
    statuses = []

    def handler(request):
        nonlocal exists
        path = request.url.params["path"]
        if path in ("app:/", "app:/Stage"):
            assert request.method == "GET"
            return httpx.Response(200, json={"type": "dir"})
        assert path == "app:/Stage/Order"
        if request.method == "GET":
            with lock:
                seen = exists
            if not seen:
                barrier.wait(timeout=5)
                return httpx.Response(404)
            return httpx.Response(200, json={"type": "dir"})
        assert request.method == "PUT"
        with lock:
            status = 409 if exists else 201
            exists = True
            statuses.append(status)
        return httpx.Response(status)

    with install(monkeypatch, handler), ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: main.ensure_yandex_folder("synthetic", "app:/Stage/Order"), range(2)))
    assert sorted(statuses) == [201, 409]


@pytest.mark.parametrize("metadata", [{"type": "file"}, {}, [], None])
def test_existing_resource_must_be_directory(monkeypatch, metadata):
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=metadata)
    with install(monkeypatch, handler), pytest.raises(RuntimeError, match="directory"):
        main.ensure_yandex_folder("synthetic", "Stage/Order")
    assert calls == ["GET"]


@pytest.mark.parametrize("after_conflict", [404, {"type": "file"}, {}, "invalid-json"])
def test_409_requires_confirmed_directory(monkeypatch, after_conflict):
    lookups = 0
    def handler(request):
        nonlocal lookups
        if request.method == "PUT":
            return httpx.Response(409)
        lookups += 1
        if lookups == 1 or after_conflict == 404:
            return httpx.Response(404)
        if after_conflict == "invalid-json":
            return httpx.Response(200, content=b"not-json")
        return httpx.Response(200, json=after_conflict)
    with install(monkeypatch, handler), pytest.raises(RuntimeError):
        main.ensure_yandex_folder("synthetic", "Stage/Order")
    assert lookups == 2


@pytest.mark.parametrize("method,status", [(m, s) for m in ("GET", "PUT") for s in (400, 401, 403, 429, 500, 200, 202)])
def test_api_errors_and_unconfirmed_success_do_not_continue(monkeypatch, method, status):
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == "GET" and method == "PUT":
            return httpx.Response(404)
        if status == 200:
            return httpx.Response(status, json={})
        return httpx.Response(status)
    with install(monkeypatch, handler), pytest.raises((RuntimeError, httpx.HTTPStatusError)):
        main.ensure_yandex_folder("synthetic", "Stage/Order")
    assert calls == (["GET"] if method == "GET" else ["GET", "PUT"])


@pytest.mark.parametrize("bad", ["", "app:", "trash:/Stage", "https://other/Stage", "app://Stage",
    "app:/Stage/../outside", "app:/Stage/./x", "Stage//Order", "Stage\\Order", "Stage/app:/x",
    "app:/Stage/%2e%2e/x", "app:/Stage/%252f/x", "app:/Stage/%2foutside", "Stage/\x00x", "Stage/\nx"])
def test_invalid_namespace_or_traversal_rejected_before_io(monkeypatch, bad):
    disk = Disk()
    with install(monkeypatch, disk), pytest.raises(RuntimeError):
        main.ensure_yandex_folder("synthetic", bad)
    assert disk.calls == []


@pytest.mark.parametrize("outside", ["disk:/Stage/Order", "Stage/Order", "app:/Stage_evil/Order", "app:/Other/Order"])
def test_explicit_root_cannot_be_escaped(monkeypatch, outside):
    monkeypatch.setenv("YANDEX_DISK_ROOT_DIR", "app:/Stage")
    disk = Disk()
    with install(monkeypatch, disk), pytest.raises(RuntimeError, match="outside"):
        main.ensure_yandex_folder("synthetic", outside)
    assert disk.calls == []


@pytest.mark.parametrize("filename", ["../other.jpg", "app:/other.jpg", "x/y.jpg", "x\\y.jpg", ".", "..", "%2fother"])
def test_remote_filename_is_one_safe_component(tmp_path, monkeypatch, filename):
    source = tmp_path / "1_photo.jpg"
    source.write_bytes(b"synthetic")
    disk = Disk()
    with install(monkeypatch, disk), pytest.raises(RuntimeError):
        main.upload_to_yandex_disk(str(source), "app:/Stage", filename)
    assert disk.calls == [] and source.read_bytes() == b"synthetic"


def test_explicit_disk_namespace_cannot_bypass_staging_production_guard(monkeypatch):
    monkeypatch.setenv("PHOTODOC_STAGING", "1")
    monkeypatch.setenv("YANDEX_DISK_ROOT_DIR", "disk:/PhotoDoc_Orders")
    with pytest.raises(RuntimeError, match="forbidden"):
        main.validate_staging_delivery_safety()


def test_app_root_order_paths_keep_namespace_without_double_slashes(tmp_path, monkeypatch):
    monkeypatch.setenv("YANDEX_DISK_ROOT_DIR", "app:/")
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path / "uploads")
    main.UPLOAD_DIR.mkdir()
    main.init_db()
    with TestClient(main.app) as client:
        result = client.post("/api/order", data={"name": "Synthetic", "phone": "79990000000",
            "format": "10x15", "paper": "Матовая", "crop": "Без полей"},
            files={"files": ("photo.jpg", b"\xff\xd8\xffsynthetic", "image/jpeg")})
    assert result.status_code == 200
    with sqlite3.connect(main.DB_PATH) as c:
        paths = [r[0] for r in c.execute("SELECT remote_path FROM delivery_jobs WHERE kind IN ('file','info')")]
    assert len(paths) == 2 and all(p.startswith("app:/Заказ_") and "//" not in p for p in paths)


def test_interrupted_upload_retains_source_and_native_retry_verifies_before_cleanup(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("YANDEX_DISK_ROOT_DIR", "app:/Stage")
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path / "uploads")
    main.UPLOAD_DIR.mkdir()
    main.init_db()
    source = main.UPLOAD_DIR / "1_photo.jpg"
    content = b"synthetic-photo" * 6000
    source.write_bytes(content)
    target = "app:/Stage/Order/1_photo.jpg"
    with sqlite3.connect(main.DB_PATH) as c:
        c.execute("INSERT INTO orders(id,name,payment_status) VALUES(1,'Synthetic','paid')")
        delivery.enqueue(c, order_id=1, kind="file", local_path=str(source), remote_path=target,
            sha256=hashlib.sha256(content).hexdigest(), size=len(content))
    disk = Disk({"app:/", "app:/Stage"})
    disk.interrupt = True
    with install(monkeypatch, disk):
        delivery.process_due(main.DB_PATH, main.deliver_job, limit=1, uploads_dir=main.UPLOAD_DIR)
        assert disk.partial and source.read_bytes() == content and target not in disk.files
        with sqlite3.connect(main.DB_PATH) as c:
            assert c.execute("SELECT state,local_deleted_at FROM delivery_jobs").fetchone() == ("failed", None)
            c.execute("UPDATE delivery_jobs SET next_attempt_at=?", (delivery.utc_now(),))
        delivery.process_due(main.DB_PATH, main.deliver_job, limit=1, uploads_dir=main.UPLOAD_DIR)
        delivery.process_due(main.DB_PATH, main.deliver_job, limit=1, uploads_dir=main.UPLOAD_DIR)
    assert disk.files == {target: content} and disk.committed_uploads == 1 and disk.upload_attempts == 2
    assert not source.exists()
    with sqlite3.connect(main.DB_PATH) as c:
        assert c.execute("SELECT state,cleanup_state FROM delivery_jobs").fetchone() == ("delivered", "completed")
        assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    assert "synthetic-private-error" not in caplog.text and "synthetic-namespace-token" not in caplog.text
