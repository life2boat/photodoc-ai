#!/usr/bin/env python3
"""PhotoDoc AI — R2 Public Launch & Staging Preflight Checker.

Validates DNS resolution, TLS certificates, HTTP->HTTPS redirects, API health,
and port isolation before public launch.
"""

from __future__ import annotations

import argparse
import datetime
import json
import socket
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


def check_dns(domain: str, expected_ip: Optional[str] = None) -> Dict[str, Any]:
    """Check DNS A record resolution for the target domain."""
    result: Dict[str, Any] = {
        "domain": domain,
        "resolved": False,
        "ips": [],
        "matches_expected": None,
        "error": None,
    }
    try:
        _, _, ip_list = socket.gethostbyname_ex(domain)
        result["resolved"] = bool(ip_list)
        result["ips"] = ip_list
        if expected_ip:
            result["matches_expected"] = expected_ip in ip_list
    except Exception as exc:
        result["error"] = str(exc)
    return result


def check_http_redirect(domain: str, port: int = 80, timeout: float = 5.0) -> Dict[str, Any]:
    """Verify that HTTP port 80 issues a 301/308 redirect to HTTPS."""
    result: Dict[str, Any] = {
        "url": f"http://{domain}:{port}/",
        "connects": False,
        "redirects_to_https": False,
        "status_code": None,
        "location": None,
        "error": None,
    }

    class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirectHandler)
    req = urllib.request.Request(
        f"http://{domain}:{port}/",
        headers={"User-Agent": "PhotoDoc-Staging-Preflight/1.0"},
    )

    try:
        with opener.open(req, timeout=timeout) as resp:
            result["connects"] = True
            result["status_code"] = resp.status
    except urllib.error.HTTPError as exc:
        result["connects"] = True
        result["status_code"] = exc.code
        loc = exc.headers.get("Location", "")
        result["location"] = loc
        if exc.code in (301, 302, 307, 308) and loc.startswith("https://"):
            result["redirects_to_https"] = True
    except Exception as exc:
        result["error"] = str(exc)

    return result


def check_tls_certificate(
    domain: str,
    port: int = 443,
    timeout: float = 5.0,
    verify: bool = True,
) -> Dict[str, Any]:
    """Inspect the TLS certificate on port 443 for validity, SAN, and expiration."""
    result: Dict[str, Any] = {
        "domain": domain,
        "port": port,
        "handshake_success": False,
        "tls_version": None,
        "cipher": None,
        "issuer": None,
        "subject": None,
        "san": [],
        "not_after": None,
        "days_remaining": None,
        "is_expired": None,
        "error": None,
    }

    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    try:
        with socket.create_connection((domain, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                result["handshake_success"] = True
                result["tls_version"] = ssock.version()
                result["cipher"] = ssock.cipher()
                cert = ssock.getpeercert() or {}

                # Subject and Issuer
                sub_dict = dict(x[0] for x in cert.get("subject", []))
                iss_dict = dict(x[0] for x in cert.get("issuer", []))
                result["subject"] = sub_dict.get("commonName")
                result["issuer"] = iss_dict.get("commonName")

                # Subject Alternative Names (SAN)
                sans = [v for k, v in cert.get("subjectAltName", []) if k == "DNS"]
                result["san"] = sans

                # Expiration calculation
                not_after_str = cert.get("notAfter")
                if not_after_str:
                    # e.g., 'May 23 01:11:25 2027 GMT'
                    not_after_dt = datetime.datetime.strptime(
                        not_after_str, "%b %d %H:%M:%S %Y %Z"
                    ).replace(tzinfo=datetime.timezone.utc)
                    result["not_after"] = not_after_dt.isoformat()
                    now = datetime.datetime.now(datetime.timezone.utc)
                    remaining = (not_after_dt - now).total_seconds() / 86400.0
                    result["days_remaining"] = round(remaining, 1)
                    result["is_expired"] = remaining <= 0
    except Exception as exc:
        result["error"] = str(exc)

    return result


def check_api_health(
    base_url: str,
    timeout: float = 5.0,
    verify: bool = True,
) -> Dict[str, Any]:
    """Verify that /api/health responds with 200 OK and valid status."""
    url = f"{base_url.rstrip('/')}/api/health"
    result: Dict[str, Any] = {
        "url": url,
        "reachable": False,
        "status_code": None,
        "response_body": None,
        "healthy": False,
        "error": None,
    }

    ctx = None
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "PhotoDoc-Staging-Preflight/1.0"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            result["reachable"] = True
            result["status_code"] = resp.status
            body_bytes = resp.read()
            try:
                body_json = json.loads(body_bytes.decode("utf-8"))
                result["response_body"] = body_json
                if resp.status == 200 and body_json.get("status") in ("ok", "healthy"):
                    result["healthy"] = True
            except Exception:
                result["response_body"] = body_bytes.decode("utf-8", errors="replace")[:200]
                if resp.status == 200:
                    result["healthy"] = True
    except urllib.error.HTTPError as exc:
        result["reachable"] = True
        result["status_code"] = exc.code
        result["error"] = f"HTTP {exc.code} {exc.reason}"
    except Exception as exc:
        result["error"] = str(exc)

    return result


def check_port_isolation(ip: str, forbidden_ports: List[int], timeout: float = 2.0) -> Dict[str, Any]:
    """Verify that private application ports (8000, 8080) are NOT open publicly."""
    result: Dict[str, Any] = {
        "ip": ip,
        "all_ports_isolated": True,
        "open_ports": [],
        "closed_ports": [],
    }

    for port in forbidden_ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        code = s.connect_ex((ip, port))
        s.close()
        if code == 0:
            result["open_ports"].append(port)
            result["all_ports_isolated"] = False
        else:
            result["closed_ports"].append(port)

    return result


def run_preflight(
    domain: str,
    expected_ip: Optional[str] = None,
    verify_tls: bool = True,
    forbidden_ports: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Execute all preflight checks and determine overall PASS/FAIL readiness."""
    report: Dict[str, Any] = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "domain": domain,
        "dns": check_dns(domain, expected_ip),
        "http_redirect": {},
        "tls": {},
        "api_health": {},
        "port_isolation": {},
        "pass": False,
        "blockers": [],
    }

    # 1. DNS Check
    if not report["dns"]["resolved"]:
        report["blockers"].append(f"DNS for {domain} failed to resolve (NXDOMAIN or DNS error)")
    elif expected_ip and not report["dns"].get("matches_expected"):
        report["blockers"].append(
            f"DNS resolved to {report['dns']['ips']}, expected {expected_ip}"
        )

    # If DNS resolved, proceed with network checks
    if report["dns"]["resolved"]:
        report["http_redirect"] = check_http_redirect(domain)
        if not report["http_redirect"]["redirects_to_https"]:
            report["blockers"].append(
                f"HTTP port 80 did not issue HTTPS redirect (status={report['http_redirect'].get('status_code')})"
            )

        report["tls"] = check_tls_certificate(domain, verify=verify_tls)
        if not report["tls"]["handshake_success"]:
            report["blockers"].append(f"TLS handshake failed: {report['tls'].get('error')}")
        elif report["tls"].get("is_expired"):
            report["blockers"].append(f"TLS certificate is expired (not_after={report['tls'].get('not_after')})")

        report["api_health"] = check_api_health(f"https://{domain}", verify=verify_tls)
        if not report["api_health"]["healthy"]:
            report["blockers"].append(f"API /api/health returned non-healthy state: {report['api_health'].get('error')}")

    # 2. Port Isolation Check
    target_ip = expected_ip or (report["dns"]["ips"][0] if report["dns"]["ips"] else None)
    if target_ip and forbidden_ports:
        report["port_isolation"] = check_port_isolation(target_ip, forbidden_ports)
        if not report["port_isolation"]["all_ports_isolated"]:
            report["blockers"].append(
                f"Sensitive ports exposed publicly on {target_ip}: {report['port_isolation']['open_ports']}"
            )

    report["pass"] = len(report["blockers"]) == 0
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="PhotoDoc AI — R2 Staging Preflight Checker")
    parser.add_argument("--domain", default="photodoc-ai.ru", help="Domain to test")
    parser.add_argument("--expected-ip", help="Expected IPv4 address")
    parser.add_argument("--skip-tls-verify", action="store_true", help="Allow self-signed or invalid certs in staging")
    parser.add_argument("--forbidden-ports", nargs="*", type=int, default=[8000, 8080], help="Ports that must be closed")
    parser.add_argument("--json", action="store_true", help="Print report in JSON format")

    args = parser.parse_args()
    report = run_preflight(
        domain=args.domain,
        expected_ip=args.expected_ip,
        verify_tls=not args.skip_tls_verify,
        forbidden_ports=args.forbidden_ports,
    )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        status = "PASS" if report["pass"] else "BLOCKED"
        print(f"==================================================")
        print(f" PhotoDoc AI — R2 Preflight Check: {status}")
        print(f"==================================================")
        print(f" Domain:               {report['domain']}")
        print(f" DNS Resolved:         {report['dns']['resolved']} ({', '.join(report['dns']['ips']) or 'None'})")
        print(f" HTTP -> HTTPS 301:    {report.get('http_redirect', {}).get('redirects_to_https', 'N/A')}")
        print(f" TLS Handshake:        {report.get('tls', {}).get('handshake_success', 'N/A')}")
        print(f" TLS Days Left:        {report.get('tls', {}).get('days_remaining', 'N/A')}")
        print(f" API Health (/api):    {report.get('api_health', {}).get('healthy', 'N/A')}")
        if report.get("port_isolation"):
            print(f" Port Isolation:       {report['port_isolation']['all_ports_isolated']} (open: {report['port_isolation']['open_ports']})")

        if report["blockers"]:
            print(f"\nBlockers ({len(report['blockers'])}):")
            for b in report["blockers"]:
                print(f"  [!] {b}")
        print(f"==================================================")

    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
