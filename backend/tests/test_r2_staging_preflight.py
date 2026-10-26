import datetime
import importlib.util
import socket
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Dynamically import scripts/r2_staging_preflight.py
script_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "r2_staging_preflight.py"
spec = importlib.util.spec_from_file_location("r2_staging_preflight", script_path)
preflight = importlib.util.module_from_spec(spec)
sys.modules["r2_staging_preflight"] = preflight
spec.loader.exec_module(preflight)


def test_check_dns_success():
    with patch("socket.gethostbyname_ex", return_value=("photodoc-ai.ru", [], ["1.2.3.4"])):
        res = preflight.check_dns("photodoc-ai.ru", expected_ip="1.2.3.4")
        assert res["resolved"] is True
        assert res["ips"] == ["1.2.3.4"]
        assert res["matches_expected"] is True
        assert res["error"] is None


def test_check_dns_failure():
    with patch("socket.gethostbyname_ex", side_effect=socket.gaierror(11001, "getaddrinfo failed")):
        res = preflight.check_dns("unregistered-domain.ru")
        assert res["resolved"] is False
        assert res["ips"] == []
        assert "getaddrinfo failed" in res["error"]


def test_check_http_redirect_success():
    with patch("urllib.request.build_opener") as mock_opener:
        instance = mock_opener.return_value
        instance.open.side_effect = urllib.error.HTTPError(
            "http://photodoc-ai.ru/", 301, "Moved Permanently", {"Location": "https://photodoc-ai.ru/"}, None
        )
        res = preflight.check_http_redirect("photodoc-ai.ru")
        assert res["connects"] is True
        assert res["status_code"] == 301
        assert res["redirects_to_https"] is True


def test_check_http_redirect_fails_on_plain_200():
    mock_resp = MagicMock()
    mock_resp.status = 200
    with patch("urllib.request.build_opener") as mock_opener:
        instance = mock_opener.return_value
        instance.open.return_value.__enter__.return_value = mock_resp
        res = preflight.check_http_redirect("photodoc-ai.ru")
        assert res["connects"] is True
        assert res["status_code"] == 200
        assert res["redirects_to_https"] is False


def test_check_tls_certificate_success():
    mock_cert = {
        "subject": ((("commonName", "photodoc-ai.ru"),),),
        "issuer": ((("commonName", "Let's Encrypt Authority"),),),
        "subjectAltName": (("DNS", "photodoc-ai.ru"), ("DNS", "www.photodoc-ai.ru")),
        "notAfter": (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=60)).strftime(
            "%b %d %H:%M:%S %Y GMT"
        ),
    }

    mock_sock = MagicMock()
    mock_ssock = MagicMock()
    mock_ssock.version.return_value = "TLSv1.3"
    mock_ssock.cipher.return_value = ("TLS_AES_256_GCM_SHA384", "TLSv1.3", 256)
    mock_ssock.getpeercert.return_value = mock_cert

    with patch("socket.create_connection", return_value=mock_sock):
        with patch("ssl.create_default_context") as mock_ctx:
            mock_ctx.return_value.wrap_socket.return_value.__enter__.return_value = mock_ssock
            res = preflight.check_tls_certificate("photodoc-ai.ru")
            assert res["handshake_success"] is True
            assert res["subject"] == "photodoc-ai.ru"
            assert res["domain_matches"] is True
            assert "www.photodoc-ai.ru" in res["san"]
            assert res["days_remaining"] > 50
            assert res["is_expired"] is False


def test_check_tls_certificate_fails_on_domain_mismatch():
    mock_cert = {
        "subject": ((("commonName", "www.microsoft.com"),),),
        "issuer": ((("commonName", "Microsoft TLS CA"),),),
        "subjectAltName": (("DNS", "www.microsoft.com"),),
        "notAfter": (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=60)).strftime(
            "%b %d %H:%M:%S %Y GMT"
        ),
    }

    mock_sock = MagicMock()
    mock_ssock = MagicMock()
    mock_ssock.version.return_value = "TLSv1.3"
    mock_ssock.cipher.return_value = ("TLS_AES_256_GCM_SHA384", "TLSv1.3", 256)
    mock_ssock.getpeercert.return_value = mock_cert

    with patch("socket.create_connection", return_value=mock_sock):
        with patch("ssl.create_default_context") as mock_ctx:
            mock_ctx.return_value.wrap_socket.return_value.__enter__.return_value = mock_ssock
            res = preflight.check_tls_certificate("photodoc-ai.ru")
            assert res["handshake_success"] is True
            assert res["domain_matches"] is False


def test_check_tls_certificate_fails_on_expired_cert():
    mock_cert = {
        "subject": ((("commonName", "photodoc-ai.ru"),),),
        "issuer": ((("commonName", "Let's Encrypt"),),),
        "subjectAltName": (("DNS", "photodoc-ai.ru"),),
        "notAfter": (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)).strftime(
            "%b %d %H:%M:%S %Y GMT"
        ),
    }

    mock_sock = MagicMock()
    mock_ssock = MagicMock()
    mock_ssock.version.return_value = "TLSv1.3"
    mock_ssock.cipher.return_value = ("TLS_AES_256_GCM_SHA384", "TLSv1.3", 256)
    mock_ssock.getpeercert.return_value = mock_cert

    with patch("socket.create_connection", return_value=mock_sock):
        with patch("ssl.create_default_context") as mock_ctx:
            mock_ctx.return_value.wrap_socket.return_value.__enter__.return_value = mock_ssock
            res = preflight.check_tls_certificate("photodoc-ai.ru")
            assert res["handshake_success"] is True
            assert res["is_expired"] is True
            assert res["days_remaining"] < 0


def test_check_api_health_success_on_valid_json():
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b'{"status": "ok", "app": "PhotoDoc"}'

    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value = mock_resp
        res = preflight.check_api_health("https://photodoc-ai.ru")
        assert res["reachable"] is True
        assert res["healthy"] is True
        assert res["is_json"] is True
        assert res["response_body"]["status"] == "ok"


def test_check_api_health_fails_on_html():
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b"<!DOCTYPE html><html><body>Error 404 page</body></html>"

    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value = mock_resp
        res = preflight.check_api_health("https://photodoc-ai.ru")
        assert res["reachable"] is True
        assert res["healthy"] is False
        assert res["is_json"] is False
        assert "not valid JSON" in res["error"]


def test_check_api_health_fails_on_empty_body():
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b""

    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value = mock_resp
        res = preflight.check_api_health("https://photodoc-ai.ru")
        assert res["reachable"] is True
        assert res["healthy"] is False
        assert "Empty response body" in res["error"]


def test_check_api_health_fails_on_unexpected_status():
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b'{"status": "degraded"}'

    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value = mock_resp
        res = preflight.check_api_health("https://photodoc-ai.ru")
        assert res["reachable"] is True
        assert res["healthy"] is False
        assert "Expected JSON status 'ok'" in res["error"]


def test_check_api_health_fails_on_http_error():
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.side_effect = urllib.error.HTTPError(
            "https://photodoc-ai.ru/api/health", 502, "Bad Gateway", {}, None
        )
        res = preflight.check_api_health("https://photodoc-ai.ru")
        assert res["reachable"] is True
        assert res["healthy"] is False
        assert "HTTP 502" in res["error"]


def test_check_port_isolation_detects_open_ports():
    def mock_connect_ex(addr):
        # 8000 is open (bad), 8080 is closed (good)
        return 0 if addr[1] == 8000 else 111

    with patch("socket.socket") as mock_s:
        mock_s.return_value.connect_ex.side_effect = mock_connect_ex
        res = preflight.check_port_isolation("1.2.3.4", [8000, 8080])
        assert res["all_ports_isolated"] is False
        assert 8000 in res["open_ports"]
        assert 8080 in res["closed_ports"]


def test_run_preflight_blocks_when_tls_verify_skipped():
    with patch("socket.gethostbyname_ex", return_value=("photodoc-ai.ru", [], ["1.2.3.4"])):
        with patch.object(preflight, "check_http_redirect", return_value={"redirects_to_https": True}):
            with patch.object(preflight, "check_tls_certificate", return_value={"handshake_success": True, "domain_matches": True, "is_expired": False, "days_remaining": 30}):
                with patch.object(preflight, "check_api_health", return_value={"healthy": True}):
                    report = preflight.run_preflight("photodoc-ai.ru", verify_tls=False)
                    assert report["pass"] is False
                    assert any("TLS verification disabled" in b for b in report["blockers"])
