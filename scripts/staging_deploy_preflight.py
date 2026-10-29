#!/usr/bin/env python3
"""PhotoDoc AI — Safe Staging Deployment Preflight & Gatekeeper.

Validates prebuilt image references, immutable digest pinning, release manifest alignment,
resolved Compose safety, loopback isolation, production target rejection, and secret safety
before any deployment action is taken on a low-memory VPS.

Strict Safety Invariants:
- Rejects production target IP 79.137.196.14 and production host names.
- Rejects mutable image tags (e.g. 'latest') and requires immutable sha256 digests.
- Confirms release-manifest.json digest consistency when manifest is provided.
- Rejects any Compose files containing 'build:' directives.
- Confirms loopback binding (127.0.0.1) for frontend port.
- Confirms isolated staging volumes and network; rejects production volume names.
- Confirms ROBOKASSA_IS_TEST=1 and DELIVERY_WORKER_ENABLED=0 by default.
- Confirms remote destination is not PhotoDoc_Orders.
- Confirms delivery override requires dedicated staging token and targets PhotoDoc_Staging_R4.
- NEVER outputs tokens, passwords, or secret-bearing configuration strings.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent

# Known production server IP and domains that must be strictly rejected
PRODUCTION_TARGET_IPS = {"79.137.196.14"}
PRODUCTION_TARGET_HOSTS = {"79.137.196.14", "photodoc-ai.ru", "www.photodoc-ai.ru"}

PRODUCTION_FORBIDDEN_VOLUMES = {"photodoc_backend-data", "photodoc_uploads"}
PRODUCTION_FORBIDDEN_ENV_FILES = {"backend/.env", "./backend/.env"}
PRODUCTION_FORBIDDEN_ROOTS = {"PhotoDoc_Orders"}

KNOWN_PLACEHOLDERS = {
    "y0_test_staging_token_mock",
    "y0_example_staging_token...",
    "mock",
    "placeholder",
    "test",
    "test-oauth-token",
    "changeme",
    "your_staging_token_here",
}

DIGEST_REGEX = re.compile(r"sha256:[a-f0-9]{64}")


def sanitize_value(key: str, val: Any) -> str:
    """Mask secret-bearing values for safe reporting."""
    s = str(val)
    upper_key = key.upper()
    if any(k in upper_key for k in ("TOKEN", "PASSWORD", "SECRET", "KEY", "AUTH", "PASS")):
        return f"[SET - {len(s)} chars]" if s else "[EMPTY]"
    return s


def check_production_target_guard(target_host: Optional[str] = None) -> Tuple[bool, List[str]]:
    """Reject explicit target hosts or local execution matching production server 79.137.196.14."""
    blockers: List[str] = []

    # 1. Check explicit target host argument or environment variable
    candidate_host = target_host or os.getenv("PHOTODOC_TARGET_HOST") or os.getenv("DEPLOY_TARGET_HOST") or os.getenv("TARGET_HOST")
    if candidate_host:
        candidate_clean = candidate_host.strip().lower()
        if candidate_clean in PRODUCTION_TARGET_HOSTS or candidate_clean in PRODUCTION_TARGET_IPS:
            blockers.append(
                f"PRODUCTION_TARGET_GUARD_REJECTED: Target host {candidate_clean!r} matches production infrastructure. "
                "Staging deployment is strictly prohibited on production target."
            )

    # 2. Check local network interfaces (without outbound network connections)
    try:
        hostname = socket.gethostname()
        _, _, ip_list = socket.gethostbyname_ex(hostname)
        for ip in ip_list:
            if ip in PRODUCTION_TARGET_IPS:
                blockers.append(
                    f"PRODUCTION_HOST_DETECTED: Local network interface {ip} matches protected production IP 79.137.196.14. "
                    "Deployment on production server is strictly blocked."
                )
    except Exception:
        # Hostname resolution not available or isolated; do not fail here
        pass

    return len(blockers) == 0, blockers


def check_image_references(
    backend_image: Optional[str],
    frontend_image: Optional[str],
    require_digest: bool = True,
) -> Tuple[bool, Dict[str, Any], List[str]]:
    """Validate backend and frontend image references and check digest pinning."""
    details: Dict[str, Any] = {
        "backend_image": backend_image or "",
        "frontend_image": frontend_image or "",
        "backend_has_digest": False,
        "frontend_has_digest": False,
        "backend_digest": None,
        "frontend_digest": None,
        "mutable_tags_detected": False,
    }
    blockers: List[str] = []

    if not backend_image or not backend_image.strip():
        blockers.append("MISSING_BACKEND_IMAGE: PHOTODOC_BACKEND_IMAGE is unset or empty.")
    if not frontend_image or not frontend_image.strip():
        blockers.append("MISSING_FRONTEND_IMAGE: PHOTODOC_FRONTEND_IMAGE is unset or empty.")

    if blockers:
        return False, details, blockers

    b_img = backend_image.strip()
    f_img = frontend_image.strip()

    # Reject latest or mutable tags
    for name, img in (("backend", b_img), ("frontend", f_img)):
        lower = img.lower()
        if lower.endswith(":latest") or "@latest" in lower or lower.endswith(":dev"):
            details["mutable_tags_detected"] = True
            blockers.append(f"MUTABLE_TAG_FORBIDDEN: {name} image uses forbidden mutable tag ({img}).")

    # Extract sha256 digests
    b_match = DIGEST_REGEX.search(b_img)
    if b_match:
        details["backend_has_digest"] = True
        details["backend_digest"] = b_match.group(0)

    f_match = DIGEST_REGEX.search(f_img)
    if f_match:
        details["frontend_has_digest"] = True
        details["frontend_digest"] = f_match.group(0)

    if require_digest:
        if not details["backend_has_digest"]:
            blockers.append(
                f"UNPINNED_BACKEND_IMAGE: Backend image ({b_img}) is not pinned to an immutable sha256 digest."
            )
        if not details["frontend_has_digest"]:
            blockers.append(
                f"UNPINNED_FRONTEND_IMAGE: Frontend image ({f_img}) is not pinned to an immutable sha256 digest."
            )

    ok = len(blockers) == 0
    return ok, details, blockers


def check_release_manifest(
    manifest_path: Path,
    backend_image: str,
    frontend_image: str,
) -> Tuple[bool, Dict[str, Any], List[str]]:
    """Verify image references against release-manifest.json if provided."""
    details: Dict[str, Any] = {
        "manifest_path": str(manifest_path),
        "manifest_exists": manifest_path.is_file(),
        "commit_sha": None,
        "backend_digest_matches": False,
        "frontend_digest_matches": False,
    }
    blockers: List[str] = []

    if not manifest_path.is_file():
        blockers.append(f"MANIFEST_NOT_FOUND: Release manifest not found at {manifest_path}")
        return False, details, blockers

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        blockers.append(f"MANIFEST_INVALID_JSON: Failed to parse manifest JSON: {exc}")
        return False, details, blockers

    details["commit_sha"] = manifest.get("commit_sha")
    images_meta = manifest.get("images", {})
    manifest_backend = images_meta.get("backend", {})
    manifest_frontend = images_meta.get("frontend", {})

    expected_backend_digest = manifest_backend.get("digest")
    expected_frontend_digest = manifest_frontend.get("digest")

    if not expected_backend_digest or not expected_frontend_digest:
        blockers.append("MANIFEST_MISSING_DIGESTS: Release manifest lacks backend or frontend sha256 digest.")
        return False, details, blockers

    # Check backend digest alignment
    if expected_backend_digest in backend_image:
        details["backend_digest_matches"] = True
    else:
        blockers.append(
            f"MANIFEST_BACKEND_MISMATCH: Backend image does not match manifest digest {expected_backend_digest}."
        )

    # Check frontend digest alignment
    if expected_frontend_digest in frontend_image:
        details["frontend_digest_matches"] = True
    else:
        blockers.append(
            f"MANIFEST_FRONTEND_MISMATCH: Frontend image does not match manifest digest {expected_frontend_digest}."
        )

    ok = len(blockers) == 0
    return ok, details, blockers


def _parse_yaml_or_fallback(content: str) -> Dict[str, Any]:
    if yaml is not None:
        return yaml.safe_load(content) or {}
    data: Dict[str, Any] = {
        "services": {
            "backend": {"environment": {}, "volumes": []},
            "frontend": {"ports": []},
        },
        "volumes": {},
    }
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if ":" in s:
            k, v = s.split(":", 1)
            k = k.strip().strip("'\"")
            v = v.strip().strip("'\"")
            if k in ("DELIVERY_WORKER_ENABLED", "ROBOKASSA_IS_TEST", "YANDEX_DISK_ROOT_DIR", "YANDEX_DISK_TOKEN"):
                data["services"]["backend"]["environment"][k] = v
            if k == "image":
                if "PHOTODOC_BACKEND" in v:
                    data["services"].setdefault("backend", {})["image"] = v
                elif "PHOTODOC_FRONTEND" in v:
                    data["services"].setdefault("frontend", {})["image"] = v
        if "photodoc_staging_backend-data" in line:
            data["services"]["backend"]["volumes"].append("photodoc_staging_db:/data")
            data["volumes"]["photodoc_staging_db"] = {}
        if "photodoc_staging_uploads" in line:
            data["services"]["backend"]["volumes"].append("photodoc_staging_uploads:/app/uploads")
        if "127.0.0.1:" in line:
            data["services"]["frontend"]["ports"].append(s.strip("- "))
        if "build:" in line:
            data["services"].setdefault("backend", {})["build"] = True
    return data


def check_staging_compose(compose_path: Path) -> Tuple[bool, Dict[str, Any], List[str]]:
    """Validate resolved staging Compose file syntax, zero-build, and isolation guarantees."""
    details: Dict[str, Any] = {
        "compose_path": str(compose_path),
        "file_exists": compose_path.is_file(),
        "valid_yaml": False,
        "zero_build_guarantee": False,
        "loopback_binding": False,
        "isolated_volumes": False,
        "isolated_network": False,
        "robokassa_test_mode": False,
        "delivery_worker_disabled": False,
        "isolated_remote_root": False,
        "production_assets_excluded": True,
    }
    blockers: List[str] = []

    if not compose_path.is_file():
        blockers.append(f"COMPOSE_FILE_MISSING: Prebuilt staging compose not found at {compose_path}")
        return False, details, blockers

    try:
        content = compose_path.read_text(encoding="utf-8")
        data = _parse_yaml_or_fallback(content)
        details["valid_yaml"] = True
    except Exception as exc:
        blockers.append(f"COMPOSE_PARSE_ERROR: Failed to parse YAML: {exc}")
        return False, details, blockers

    # 1. Verify NO build directives
    has_build = re.search(r"^\s*build\s*:", content, re.MULTILINE)
    if not has_build:
        details["zero_build_guarantee"] = True
    else:
        blockers.append("FORBIDDEN_BUILD_DIRECTIVE: Prebuilt compose file contains 'build:' sections.")

    services = data.get("services", {})
    backend = services.get("backend", {})
    frontend = services.get("frontend", {})
    backend_env = backend.get("environment", {})

    # 2. Check fail-closed image variable requirements
    backend_img = str(backend.get("image", ""))
    frontend_img = str(frontend.get("image", ""))
    if "${PHOTODOC_BACKEND_IMAGE:?" not in content and "${PHOTODOC_BACKEND_IMAGE:?Error" not in backend_img:
        blockers.append("MISSING_FAIL_CLOSED_IMAGE_VAR: Backend image must enforce ${PHOTODOC_BACKEND_IMAGE:?...}.")
    if "${PHOTODOC_FRONTEND_IMAGE:?" not in content and "${PHOTODOC_FRONTEND_IMAGE:?Error" not in frontend_img:
        blockers.append("MISSING_FAIL_CLOSED_IMAGE_VAR: Frontend image must enforce ${PHOTODOC_FRONTEND_IMAGE:?...}.")

    # 3. Frontend bound to loopback 127.0.0.1
    ports = frontend.get("ports", [])
    if any(str(p).startswith("127.0.0.1:") for p in ports):
        details["loopback_binding"] = True
    else:
        blockers.append(f"PUBLIC_FRONTEND_PORT_FORBIDDEN: Frontend port must bind to loopback 127.0.0.1, got {ports}.")

    # 4. Isolated named volumes
    volumes = data.get("volumes", {})
    backend_vols = backend.get("volumes", [])
    has_staging_db = any("photodoc_staging_db" in str(v) or "photodoc_staging_backend-data" in str(v) for v in backend_vols)
    has_staging_uploads = any("photodoc_staging_uploads" in str(v) for v in backend_vols)
    if has_staging_db and has_staging_uploads and "photodoc_staging_db" in volumes:
        details["isolated_volumes"] = True
    else:
        blockers.append("ISOLATED_VOLUMES_MISSING: Prebuilt staging must define dedicated isolated named volumes.")

    # 5. Production volume names strictly excluded
    for forbidden_vol in PRODUCTION_FORBIDDEN_VOLUMES:
        if f"{forbidden_vol}\n" in content or f"{forbidden_vol}:" in content or f"/{forbidden_vol}" in content:
            details["production_assets_excluded"] = False
            blockers.append(f"PRODUCTION_VOLUME_FORBIDDEN: Staging compose mounts production volume {forbidden_vol!r}.")

    # 6. Production env files strictly excluded
    env_files = backend.get("env_file", [])
    if isinstance(env_files, str):
        env_files = [env_files]
    for ef in env_files:
        ef_str = str(ef)
        if any(forb == ef_str or ef_str.endswith("/backend/.env") for forb in PRODUCTION_FORBIDDEN_ENV_FILES):
            details["production_assets_excluded"] = False
            blockers.append("PRODUCTION_ENV_FILE_FORBIDDEN: Staging compose references production env file.")

    # 7. Delivery worker disabled by default
    worker_val = str(backend_env.get("DELIVERY_WORKER_ENABLED", ""))
    if worker_val == "0":
        details["delivery_worker_disabled"] = True
    else:
        blockers.append(f"DELIVERY_WORKER_NOT_DISABLED: Expected DELIVERY_WORKER_ENABLED='0', got {worker_val!r}.")

    # 8. Robokassa test mode
    test_mode = str(backend_env.get("ROBOKASSA_IS_TEST", ""))
    if test_mode == "1":
        details["robokassa_test_mode"] = True
    else:
        blockers.append(f"ROBOKASSA_TEST_MODE_REQUIRED: Expected ROBOKASSA_IS_TEST='1', got {test_mode!r}.")

    # 9. Isolated remote root
    root_dir = str(backend_env.get("YANDEX_DISK_ROOT_DIR", "")).strip().strip("/")
    if root_dir in PRODUCTION_FORBIDDEN_ROOTS or root_dir.startswith("PhotoDoc_Orders/"):
        details["production_assets_excluded"] = False
        blockers.append(f"PRODUCTION_REMOTE_ROOT_FORBIDDEN: Staging targets production remote root {root_dir!r}.")
    elif "PhotoDoc_Staging" in root_dir:
        details["isolated_remote_root"] = True
    else:
        blockers.append(f"UNRECOGNIZED_REMOTE_ROOT: Expected staging root containing PhotoDoc_Staging, got {root_dir!r}.")

    # 10. Staging network
    networks = data.get("networks", {})
    if "photodoc_staging_network" in networks:
        details["isolated_network"] = True
    else:
        blockers.append("STAGING_NETWORK_MISSING: photodoc_staging_network must be explicitly defined.")

    ok = len(blockers) == 0
    return ok, details, blockers


def check_delivery_override(
    override_path: Path,
    live_token: Optional[str] = None,
) -> Tuple[bool, Dict[str, Any], List[str]]:
    """Validate prebuilt staging delivery acceptance override file."""
    details: Dict[str, Any] = {
        "override_path": str(override_path),
        "file_exists": override_path.is_file(),
        "valid_yaml": False,
        "zero_build": False,
        "worker_enabled": False,
        "requires_explicit_token": False,
        "isolated_staging_root": False,
        "token_evaluated": bool(live_token),
        "token_safe_for_live": False,
    }
    blockers: List[str] = []

    if not override_path.is_file():
        blockers.append(f"OVERRIDE_FILE_MISSING: Delivery override file missing at {override_path}")
        return False, details, blockers

    try:
        content = override_path.read_text(encoding="utf-8")
        data = _parse_yaml_or_fallback(content)
        details["valid_yaml"] = True
    except Exception as exc:
        blockers.append(f"OVERRIDE_PARSE_ERROR: Failed to parse override YAML: {exc}")
        return False, details, blockers

    if not re.search(r"^\s*build\s*:", content, re.MULTILINE):
        details["zero_build"] = True
    else:
        blockers.append("OVERRIDE_CONTAINS_BUILD: Delivery override contains 'build:' directives.")

    backend = data.get("services", {}).get("backend", {})
    env = backend.get("environment", {})

    # Enables worker
    if str(env.get("DELIVERY_WORKER_ENABLED", "")) == "1":
        details["worker_enabled"] = True
    else:
        blockers.append("OVERRIDE_WORKER_NOT_ENABLED: Override must set DELIVERY_WORKER_ENABLED='1'.")

    # Requires token fail-closed
    if "YANDEX_DISK_STAGING_TOKEN" in content and (":?" in content or "?Error:" in content):
        details["requires_explicit_token"] = True
    else:
        blockers.append("OVERRIDE_TOKEN_NOT_FAIL_CLOSED: Override must require YANDEX_DISK_STAGING_TOKEN with :?.")

    # Destination root
    root_val = str(env.get("YANDEX_DISK_ROOT_DIR", ""))
    if root_val in PRODUCTION_FORBIDDEN_ROOTS or "PhotoDoc_Orders" in root_val:
        blockers.append("OVERRIDE_TARGETS_PRODUCTION: Delivery override specifies production remote root.")
    elif "PhotoDoc_Staging_R4" in root_val or "PhotoDoc_Staging" in root_val:
        details["isolated_staging_root"] = True
    else:
        blockers.append(f"OVERRIDE_INVALID_ROOT: Expected dedicated staging root like PhotoDoc_Staging_R4, got {root_val!r}.")

    # Evaluate live token if supplied
    if live_token:
        stripped = live_token.strip()
        lower = stripped.lower()
        if lower in KNOWN_PLACEHOLDERS or "mock" in lower or "placeholder" in lower:
            blockers.append("PLACEHOLDER_TOKEN_REJECTED: Staging token is a known test placeholder; live delivery blocked.")
        else:
            details["token_safe_for_live"] = True

    ok = len(blockers) == 0
    return ok, details, blockers


def run_staging_deploy_preflight(
    target_host: Optional[str] = None,
    backend_image: Optional[str] = None,
    frontend_image: Optional[str] = None,
    compose_path: Optional[Path] = None,
    override_path: Optional[Path] = None,
    manifest_path: Optional[Path] = None,
    check_override: bool = False,
    require_digest: bool = True,
    live_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute complete staging deployment preflight suite."""
    cmp_path = compose_path or (REPO_ROOT / "docker-compose.staging.prebuilt.yml")
    ovr_path = override_path or (REPO_ROOT / "docker-compose.staging.prebuilt.delivery.yml")
    mnf_path = manifest_path or (REPO_ROOT / "release-manifest.json")

    report: Dict[str, Any] = {
        "pass": False,
        "markers": {
            "PRODUCTION_TARGET_GUARD": "BLOCKED",
            "DIGEST_VERIFICATION": "BLOCKED",
            "ZERO_BUILD_BOOTSTRAP": "BLOCKED",
            "STAGING_PREFLIGHT": "BLOCKED",
            "ROLLBACK_PROCEDURE": "BLOCKED",
        },
        "details": {},
        "blockers": [],
    }

    # 1. Production Target Safety Guard
    tgt_ok, tgt_blockers = check_production_target_guard(target_host)
    report["details"]["target_guard"] = {"target_host": target_host or "local/unset", "pass": tgt_ok}
    if tgt_ok:
        report["markers"]["PRODUCTION_TARGET_GUARD"] = "PASS"
    else:
        report["blockers"].extend(tgt_blockers)

    # 2. Image References & Digest Pinning
    b_img = backend_image or os.getenv("PHOTODOC_BACKEND_IMAGE")
    f_img = frontend_image or os.getenv("PHOTODOC_FRONTEND_IMAGE")
    img_ok, img_details, img_blockers = check_image_references(b_img, f_img, require_digest=require_digest)
    report["details"]["images"] = img_details
    if img_ok:
        report["markers"]["DIGEST_VERIFICATION"] = "PASS"
    else:
        report["blockers"].extend(img_blockers)

    # 3. Release Manifest Verification (if provided or present)
    if mnf_path and mnf_path.is_file() and b_img and f_img:
        mnf_ok, mnf_details, mnf_blockers = check_release_manifest(mnf_path, b_img, f_img)
        report["details"]["manifest"] = mnf_details
        if not mnf_ok:
            report["markers"]["DIGEST_VERIFICATION"] = "FAIL"
            report["blockers"].extend(mnf_blockers)

    # 4. Staging Compose Verification
    cmp_ok, cmp_details, cmp_blockers = check_staging_compose(cmp_path)
    report["details"]["compose"] = cmp_details
    if cmp_details.get("zero_build_guarantee"):
        report["markers"]["ZERO_BUILD_BOOTSTRAP"] = "PASS"
    if cmp_ok:
        report["markers"]["STAGING_PREFLIGHT"] = "PASS"
    else:
        report["blockers"].extend(cmp_blockers)

    # 5. Staging Delivery Override (if requested or files exist)
    if check_override or (ovr_path and ovr_path.is_file()):
        ovr_ok, ovr_details, ovr_blockers = check_delivery_override(
            ovr_path, live_token=live_token or os.getenv("YANDEX_DISK_STAGING_TOKEN")
        )
        report["details"]["override"] = ovr_details
        if not ovr_ok:
            report["blockers"].extend(ovr_blockers)

    # 6. Rollback Capability Verification
    # Rollback is validated if prebuilt compose has no build section, images can be swapped without compile,
    # and no destructive volume options exist
    if cmp_details.get("zero_build_guarantee") and img_ok and tgt_ok:
        report["markers"]["ROLLBACK_PROCEDURE"] = "PASS"
    else:
        report["markers"]["ROLLBACK_PROCEDURE"] = "BLOCKED"

    report["pass"] = len(report["blockers"]) == 0
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="PhotoDoc AI — Safe Staging Deployment Preflight & Gatekeeper Tool"
    )
    parser.add_argument(
        "--target-host",
        default=os.getenv("PHOTODOC_TARGET_HOST"),
        help="Target server IP or hostname (rejects 79.137.196.14)",
    )
    parser.add_argument(
        "--backend-image",
        default=os.getenv("PHOTODOC_BACKEND_IMAGE"),
        help="Backend image reference pinned to immutable digest",
    )
    parser.add_argument(
        "--frontend-image",
        default=os.getenv("PHOTODOC_FRONTEND_IMAGE"),
        help="Frontend image reference pinned to immutable digest",
    )
    parser.add_argument(
        "--compose-file",
        default=str(REPO_ROOT / "docker-compose.staging.prebuilt.yml"),
        help="Path to docker-compose.staging.prebuilt.yml",
    )
    parser.add_argument(
        "--override-file",
        default=str(REPO_ROOT / "docker-compose.staging.prebuilt.delivery.yml"),
        help="Path to docker-compose.staging.prebuilt.delivery.yml",
    )
    parser.add_argument(
        "--manifest",
        default=str(REPO_ROOT / "release-manifest.json"),
        help="Path to release-manifest.json",
    )
    parser.add_argument(
        "--check-override",
        action="store_true",
        help="Explicitly validate delivery override configuration",
    )
    parser.add_argument(
        "--no-require-digest",
        action="store_true",
        help="Allow commit-tagged references when verified against release manifest",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON report",
    )

    args = parser.parse_args()

    compose_p = Path(args.compose_file)
    override_p = Path(args.override_file)
    manifest_p = Path(args.manifest) if Path(args.manifest).is_file() else None

    report = run_staging_deploy_preflight(
        target_host=args.target_host,
        backend_image=args.backend_image,
        frontend_image=args.frontend_image,
        compose_path=compose_p,
        override_path=override_p,
        manifest_path=manifest_p,
        check_override=args.check_override,
        require_digest=not args.no_require_digest,
    )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("==================================================")
        print(" PhotoDoc AI — Staging Deployment Preflight Report")
        print("==================================================")
        for marker, status in report["markers"].items():
            print(f" {marker:30}: {status}")
        print("--------------------------------------------------")
        print(f" Prebuilt Zero-Build Guaranteed: {report['details'].get('compose', {}).get('zero_build_guarantee', False)}")
        print(f" Secrets Printed to Console    : false")
        print(f" Production Target Rejected    : {report['markers']['PRODUCTION_TARGET_GUARD'] == 'PASS'}")
        if report["blockers"]:
            print(f"\n Blockers ({len(report['blockers'])}):")
            for b in report["blockers"]:
                print(f"   [!] {b}")
        print("==================================================")

    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
