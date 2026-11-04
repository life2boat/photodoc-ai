"""Independent public-API/queue concurrency rehearsal; no production credentials."""
import hashlib
import sqlite3
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx
from fastapi.testclient import TestClient
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
import delivery


def test_mixed_paid_unpaid_four_workers_lock_contention_and_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(main,"DB_PATH",tmp_path/"orders.db")
    monkeypatch.setattr(main,"UPLOAD_DIR",tmp_path/"uploads")
    main.UPLOAD_DIR.mkdir()
    for k,v in {"DELIVERY_WORKER_ENABLED":"0","PHOTODOC_STAGING":"0","PHOTODOC_ALLOW_FRESH_DB":"0",
                "YANDEX_DISK_ROOT_DIR":"PhotoDoc_Orders","YANDEX_DISK_TOKEN":"synthetic-independent",
                "ROBOKASSA_PASSWORD2":"synthetic-p2","SMTP_SERVER":"smtp.example.invalid",
                "SMTP_USER":"owner@example.invalid","SMTP_PASSWORD":"synthetic","EMAIL_TO":"operator@example.invalid"}.items():
        monkeypatch.setenv(k,v)
    main.init_db()
    monkeypatch.setattr(main,"send_order_email",lambda *a:True)
    paid,unpaid=[],[]
    with TestClient(main.app) as client:
        for i in range(8):
            r=client.post("/api/order",data={"name":"Synthetic","phone":"79990000000","format":"10x15"},
                          files={"files":("synthetic.jpg",b"\xff\xd8\xffsynthetic-"+str(i).encode(),"image/jpeg")})
            assert r.status_code==200
            oid=r.json()["order_id"]
            if i%2:
                unpaid.append(oid)
            else:
                paid.append(oid)
                sig=hashlib.md5(f"20.00:{oid}:synthetic-p2".encode()).hexdigest()
                for _ in range(2):
                    assert client.post("/api/payment/robokassa/result",data={"OutSum":"20.00","InvId":str(oid),"SignatureValue":sig}).status_code==200
    guard=threading.Lock()
    directories=set(); objects={}; hrefs={}; smtp_ids=[]; puts=[]
    def handler(request):
        with guard:
            path=request.url.params.get("path")
            if request.url.host=="upload.example.invalid":
                target=hrefs[str(request.url)]
                assert request.method=="PUT" and target not in objects
                objects[target]=request.read(); puts.append(target)
                return httpx.Response(201)
            assert request.url.host=="cloud-api.yandex.net"
            if request.url.path.endswith("/upload"):
                assert request.url.params["overwrite"]=="false"
                href="https://upload.example.invalid/"+hashlib.sha256(path.encode()).hexdigest()
                hrefs[href]=path
                return httpx.Response(200,json={"href":href})
            if request.method=="GET":
                if path in directories:return httpx.Response(200,json={"type":"dir"})
                if path in objects:
                    b=objects[path]
                    return httpx.Response(200,json={"type":"file","sha256":hashlib.sha256(b).hexdigest(),"size":len(b)})
                return httpx.Response(404)
            assert request.method=="PUT"
            status=409 if path in directories else 201
            directories.add(path)
            return httpx.Response(status)
    class SMTP:
        def __init__(self,*a,**k):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def login(self,*a):pass
        def send_message(self,message):
            with guard:
                smtp_ids.append(message["Message-ID"])
                first=len(smtp_ids)==1
            if first:
                c=sqlite3.connect(main.DB_PATH,check_same_thread=False)
                c.execute("BEGIN IMMEDIATE")
                threading.Timer(0.35,lambda:(c.rollback(),c.close())).start()
            return {}
    monkeypatch.setattr(main.smtplib,"SMTP_SSL",SMTP)
    def run_worker():
        for _ in range(6):
            delivery.process_due(main.DB_PATH,main.deliver_job,limit=5,uploads_dir=main.UPLOAD_DIR)
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        monkeypatch.setattr(main.httpx,"get",transport.get)
        monkeypatch.setattr(main.httpx,"put",transport.put)
        for _ in range(2):  # Second drain models new worker instances with the same durable DB.
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures=[pool.submit(run_worker) for i in range(4)]
                for f in futures:f.result(timeout=30)
    assert len(puts)==len(set(puts))==len(paid)*2
    # Message identity follows the configured mailbox domain, not a hardcoded
    # literal. This fixture authenticates as owner@example.invalid.
    expected_domain=main.get_mail_domain()
    assert expected_domain=="example.invalid"
    assert Counter(smtp_ids)==Counter(f"<photodoc-order-{oid}-paid@{expected_domain}>" for oid in paid)
    with sqlite3.connect(main.DB_PATH) as c:
        for oid in paid:
            assert c.execute("SELECT DISTINCT state FROM delivery_jobs WHERE order_id=?",(oid,)).fetchall()==[("delivered",)]
            assert c.execute("SELECT cleanup_state FROM delivery_jobs WHERE order_id=? AND kind IN ('file','info')",(oid,)).fetchall()==[("completed",)]*2
        for oid in unpaid:
            rows=c.execute("SELECT local_path,state,attempts,local_deleted_at FROM delivery_jobs WHERE order_id=? AND kind IN ('file','info')",(oid,)).fetchall()
            assert len(rows)==2
            for path,state,attempts,deleted in rows:
                assert Path(path).is_file() and (state,attempts,deleted)==("pending",0,None)
            assert not any(f"/Заказ_{oid}_" in path for path in objects)
        assert c.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
