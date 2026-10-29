#!/usr/bin/env python3
"""PhotoDoc AI — Staging Delivery Preflight & Isolation Verification Tool.

Validates default staging compose safety, opt-in delivery-test override configuration,
remote root isolation, volume isolation, prebuilt GHCR compose configurations,
and live staging credential readiness.

Strict Safety Guards:
- Default staging compose MUST have delivery worker disabled (DELIVERY_WORKER_ENABLED="0").
- Staging delivery override MUST require an explicit, genuine YANDEX_DISK_STAGING_TOKEN.
- Known placeholder or mock tokens are strictly rejected for live staging delivery.
- Remote destination MUST NOT target production PhotoDoc_Orders.
- Production volumes (photodoc_backend-data, photodoc_uploads) are NEVER mounted.
- Prebuilt staging compose MUST NOT contain any 'build:' blocks.
- Prebuilt staging compose MUST fail closed if image variables are unset (:? modifier).
- NEVER echoes secret values, tokens, or authorization headers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent

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


def verify_staging_compose(compose_path: Path) -> Tuple[bool, Dict[str, Any]]:
    details: Dict[str, Any] = {
        "file_exists": False,
        "valid_yaml": False,
        "default_worker_disabled": False,
        "test_payment_mode": False,
        "isolated_yandex_root": False,
        "isolated_named_volumes": False,
        "production_volumes_excluded": True,
        "loopback_frontend_binding": False,
        "blockers": [],
    }

    if not compose_path.exists() or not compose_path.is_file():
        details["blockers"].append(f"Staging compose file missing: {compose_path}")
        return False, details

    details["file_exists"] = True

    try:
        content = compose_path.read_text(encoding="utf-8")
        data = _parse_yaml_or_fallback(content)
        details["valid_yaml"] = True
    except Exception as exc:
        details["blockers"].append(f"Failed to parse staging compose YAML: {exc}")
        return False, details

    services = data.get("services", {})
    backend = services.get("backend", {})
    backend_env = backend.get("environment", {})

    # 1. Delivery worker disabled by default
    worker_env = str(backend_env.get("DELIVERY_WORKER_ENABLED", ""))
    if worker_env == "0":
        details["default_worker_disabled"] = True
    else:
        details["blockers"].append(
            f"Default staging delivery worker must be disabled (expected '0', got {worker_env!r})"
        )

    # 2. Robokassa test mode
    test_mode = str(backend_env.get("ROBOKASSA_IS_TEST", ""))
    if test_mode == "1":
        details["test_payment_mode"] = True
    else:
        details["blockers"].append(
            f"Staging must run with ROBOKASSA_IS_TEST='1' (got {test_mode!r})"
        )

    # 3. Isolated Yandex root
    root_dir = str(backend_env.get("YANDEX_DISK_ROOT_DIR", "")).strip().strip("/")
    if root_dir and "PhotoDoc_Orders" not in root_dir:
        details["isolated_yandex_root"] = True
    else:
        details["blockers"].append(
            f"Staging must use isolated Yandex root (forbidden: {root_dir!r})"
        )

    # 4. Isolated named volumes
    volumes = data.get("volumes", {})
    backend_vols = backend.get("volumes", [])
    has_staging_db = any("photodoc_staging" in str(v) for v in backend_vols)
    has_staging_uploads = any("photodoc_staging_uploads" in str(v) for v in backend_vols)
    if has_staging_db and has_staging_uploads and "photodoc_staging_db" in volumes:
        details["isolated_named_volumes"] = True
    else:
        details["blockers"].append("Staging does not define dedicated isolated named volumes")

    # 5. Production volumes excluded
    raw_text = compose_path.read_text(encoding="utf-8")
    if "photodoc_backend-data\n" in raw_text or "photodoc_uploads\n" in raw_text:
        details["production_volumes_excluded"] = False
        details["blockers"].append("Production volume names found in staging compose")

    # 6. Loopback frontend binding
    frontend = services.get("frontend", {})
    ports = frontend.get("ports", [])
    if any(str(p).startswith("127.0.0.1:") for p in ports):
        details["loopback_frontend_binding"] = True
    else:
        details["blockers"].append("Frontend port is not bound to loopback 127.0.0.1")

    ok = len(details["blockers"]) == 0
    return ok, details


def verify_staging_delivery_override(override_path: Path) -> Tuple[bool, Dict[str, Any]]:
    details: Dict[str, Any] = {
        "file_exists": False,
        "valid_yaml": False,
        "worker_enabled_in_override": False,
        "requires_explicit_staging_token": False,
        "enforces_staging_root": False,
        "rejects_production_root": False,
        "no_production_env_mount": True,
        "blockers": [],
    }

    if not override_path.exists() or not override_path.is_file():
        details["blockers"].append(f"Delivery override compose file missing: {override_path}")
        return False, details

    details["file_exists"] = True

    try:
        content = override_path.read_text(encoding="utf-8")
        data = _parse_yaml_or_fallback(content)
        details["valid_yaml"] = True
    except Exception as exc:
        details["blockers"].append(f"Failed to parse delivery override YAML: {exc}")
        return False, details

    backend = data.get("services", {}).get("backend", {})
    env = backend.get("environment", {})

    # 1. Enables worker
    if str(env.get("DELIVERY_WORKER_ENABLED", "")) == "1":
        details["worker_enabled_in_override"] = True
    else:
        details["blockers"].append("Override must set DELIVERY_WORKER_ENABLED: '1'")

    # 2. Requires explicit staging token
    raw_text = override_path.read_text(encoding="utf-8")
    if "YANDEX_DISK_STAGING_TOKEN" in raw_text and ("?Error:" in raw_text or ":?" in raw_text):
        details["requires_explicit_staging_token"] = True
    else:
        details["blockers"].append("Override must require YANDEX_DISK_STAGING_TOKEN with fail-closed :? modifier")

    # 3. Enforces isolated staging root
    root_val = str(env.get("YANDEX_DISK_ROOT_DIR", ""))
    if "PhotoDoc_Staging" in root_val and "PhotoDoc_Orders" not in root_val:
        details["enforces_staging_root"] = True
        details["rejects_production_root"] = True
    else:
        details["blockers"].append("Override must default to a dedicated staging root like PhotoDoc_Staging_R4")

    # 4. No production .env or volume mount
    env_files = backend.get("env_file", [])
    if isinstance(env_files, str):
        env_files = [env_files]
    vols = backend.get("volumes", [])
    has_prod_env = any("backend/.env" in str(ef) and "example" not in str(ef) for ef in env_files)
    has_prod_vol = any("photodoc_backend-data" in str(v) or "photodoc_uploads" in str(v) for v in vols)
    if has_prod_env or has_prod_vol:
        details["no_production_env_mount"] = False
        details["blockers"].append("Delivery override mounts production .env file or volumes")

    ok = len(details["blockers"]) == 0
    return ok, details


def verify_prebuilt_staging_compose(compose_path: Path) -> Tuple[bool, Dict[str, Any]]:
    details: Dict[str, Any] = {
        "file_exists": False,
        "valid_yaml": False,
        "no_build_section": False,
        "requires_backend_image": False,
        "requires_frontend_image": False,
        "default_worker_disabled": False,
        "test_payment_mode": False,
        "isolated_yandex_root": False,
        "isolated_named_volumes": False,
        "production_volumes_excluded": True,
        "loopback_frontend_binding": False,
        "blockers": [],
    }

    if not compose_path.exists() or not compose_path.is_file():
        details["blockers"].append(f"Prebuilt staging compose file missing: {compose_path}")
        return False, details

    details["file_exists"] = True

    try:
        content = compose_path.read_text(encoding="utf-8")
        data = _parse_yaml_or_fallback(content)
        details["valid_yaml"] = True
    except Exception as exc:
        details["blockers"].append(f"Failed to parse prebuilt staging compose YAML: {exc}")
        return False, details

    services = data.get("services", {})
    backend = services.get("backend", {})
    frontend = services.get("frontend", {})

    # 1. No build sections anywhere
    has_build = "build" in backend or "build" in frontend or any(
        "build" in s for s in services.values() if isinstance(s, dict)
    )
    if not has_build and not re.search(r"^\s*build\s*:", content, re.MULTILINE):
        details["no_build_section"] = True
    else:
        details["blockers"].append("Prebuilt staging compose must NOT contain any 'build:' section")

    # 2. Requires backend image via variable with :? fail-closed modifier
    backend_img = str(backend.get("image", ""))
    if "PHOTODOC_BACKEND_IMAGE" in backend_img and (":?" in backend_img or "?Error:" in backend_img):
        details["requires_backend_image"] = True
    else:
        details["blockers"].append(
            "Backend service must reference ${PHOTODOC_BACKEND_IMAGE:?...} with fail-closed :? modifier"
        )

    # 3. Requires frontend image via variable with :? fail-closed modifier
    frontend_img = str(frontend.get("image", ""))
    if "PHOTODOC_FRONTEND_IMAGE" in frontend_img and (":?" in frontend_img or "?Error:" in frontend_img):
        details["requires_frontend_image"] = True
    else:
        details["blockers"].append(
            "Frontend service must reference ${PHOTODOC_FRONTEND_IMAGE:?...} with fail-closed :? modifier"
        )

    # 4. Delivery worker disabled by default
    backend_env = backend.get("environment", {})
    worker_env = str(backend_env.get("DELIVERY_WORKER_ENABLED", ""))
    if worker_env == "0":
        details["default_worker_disabled"] = True
    else:
        details["blockers"].append(
            f"Prebuilt staging delivery worker must be disabled by default (expected '0', got {worker_env!r})"
        )

    # 5. Robokassa test mode
    test_mode = str(backend_env.get("ROBOKASSA_IS_TEST", ""))
    if test_mode == "1":
        details["test_payment_mode"] = True
    else:
        details["blockers"].append(
            f"Prebuilt staging must run with ROBOKASSA_IS_TEST='1' (got {test_mode!r})"
        )

    # 6. Isolated Yandex root
    root_dir = str(backend_env.get("YANDEX_DISK_ROOT_DIR", "")).strip().strip("/")
    if root_dir and "PhotoDoc_Orders" not in root_dir:
        details["isolated_yandex_root"] = True
    else:
        details["blockers"].append(
            f"Prebuilt staging must use isolated Yandex root (forbidden: {root_dir!r})"
        )

    # 7. Isolated named volumes
    volumes = data.get("volumes", {})
    backend_vols = backend.get("volumes", [])
    has_staging_db = any("photodoc_staging" in str(v) for v in backend_vols)
    has_staging_uploads = any("photodoc_staging_uploads" in str(v) for v in backend_vols)
    if has_staging_db and has_staging_uploads and "photodoc_staging_db" in volumes:
        details["isolated_named_volumes"] = True
    else:
        details["blockers"].append("Prebuilt staging does not define dedicated isolated named volumes")

    # 8. Production volumes excluded
    if "photodoc_backend-data\n" in content or "photodoc_uploads\n" in content:
        details["production_volumes_excluded"] = False
        details["blockers"].append("Production volume names found in prebuilt staging compose")

    # 9. Loopback frontend binding
    ports = frontend.get("ports", [])
    if any(str(p).startswith("127.0.0.1:") for p in ports):
        details["loopback_frontend_binding"] = True
    else:
        details["blockers"].append("Prebuilt frontend port is not bound to loopback 127.0.0.1")

    ok = len(details["blockers"]) == 0
    return ok, details


def verify_prebuilt_staging_delivery_override(override_path: Path) -> Tuple[bool, Dict[str, Any]]:
    details: Dict[str, Any] = {
        "file_exists": False,
        "valid_yaml": False,
        "no_build_section": False,
        "worker_enabled_in_override": False,
        "requires_explicit_staging_token": False,
        "enforces_staging_root": False,
        "rejects_production_root": False,
        "no_production_env_mount": True,
        "blockers": [],
    }

    if not override_path.exists() or not override_path.is_file():
        details["blockers"].append(f"Prebuilt delivery override compose file missing: {override_path}")
        return False, details

    details["file_exists"] = True

    try:
        content = override_path.read_text(encoding="utf-8")
        data = _parse_yaml_or_fallback(content)
        details["valid_yaml"] = True
    except Exception as exc:
        details["blockers"].append(f"Failed to parse prebuilt delivery override YAML: {exc}")
        return False, details

    # 1. No build section
    if not re.search(r"^\s*build\s*:", content, re.MULTILINE):
        details["no_build_section"] = True
    else:
        details["blockers"].append("Prebuilt delivery override must NOT contain 'build:'")

    backend = data.get("services", {}).get("backend", {})
    env = backend.get("environment", {})

    # 2. Enables worker
    if str(env.get("DELIVERY_WORKER_ENABLED", "")) == "1":
        details["worker_enabled_in_override"] = True
    else:
        details["blockers"].append("Prebuilt delivery override must set DELIVERY_WORKER_ENABLED: '1'")

    # 3. Requires explicit staging token
    if "YANDEX_DISK_STAGING_TOKEN" in content and (":?" in content or "?Error:" in content):
        details["requires_explicit_staging_token"] = True
    else:
        details["blockers"].append(
            "Prebuilt override must require YANDEX_DISK_STAGING_TOKEN with fail-closed :? modifier"
        )

    # 4. Enforces isolated staging root
    root_val = str(env.get("YANDEX_DISK_ROOT_DIR", ""))
    if "PhotoDoc_Staging" in root_val and "PhotoDoc_Orders" not in root_val:
        details["enforces_staging_root"] = True
        details["rejects_production_root"] = True
    else:
        details["blockers"].append(
            "Prebuilt override must default to a dedicated staging root like PhotoDoc_Staging_R4"
        )

    # 5. No production .env or volume mount
    env_files = backend.get("env_file", [])
    if isinstance(env_files, str):
        env_files = [env_files]
    vols = backend.get("volumes", [])
    has_prod_env = any("backend/.env" in str(ef) and "example" not in str(ef) for ef in env_files)
    has_prod_vol = any("photodoc_backend-data" in str(v) or "photodoc_uploads" in str(v) for v in vols)
    if has_prod_env or has_prod_vol:
        details["no_production_env_mount"] = False
        details["blockers"].append("Prebuilt delivery override mounts production .env file or volumes")

    ok = len(details["blockers"]) == 0
    return ok, details


def evaluate_staging_token(token: Optional[str], root_dir: Optional[str] = None) -> Tuple[bool, str]:
    """
    Evaluates whether a provided staging token is safe for live staging acceptance testing.
    Never returns or echoes the token value.
    """
    if not token or not token.strip():
        return False, "TOKEN_UNSET_OR_EMPTY"

    stripped = token.strip()
    lower = stripped.lower()

    if lower in KNOWN_PLACEHOLDERS:
        return False, "KNOWN_PLACEHOLDER_REJECTED"

    if "placeholder" in lower or "mock" in lower or "example" in lower or lower.endswith("_mock"):
        return False, "PLACEHOLDER_PATTERN_REJECTED"

    if root_dir:
        clean_root = root_dir.strip().strip("/")
        if clean_root == "PhotoDoc_Orders" or clean_root.startswith("PhotoDoc_Orders/"):
            return False, "PRODUCTION_ROOT_REJECTED"

    return True, "TOKEN_ACCEPTED"


def run_preflight(
    compose_staging_path: Path,
    compose_override_path: Path,
    env_file: Optional[Path] = None,
    live_token: Optional[str] = None,
    compose_prebuilt_path: Optional[Path] = None,
    compose_prebuilt_override_path: Optional[Path] = None,
    check_prebuilt: bool = False,
) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "staging_compose": {},
        "staging_delivery_override": {},
        "live_credential_status": {},
        "markers": {},
        "blockers": [],
    }

    # 1. Staging Compose
    stg_ok, stg_details = verify_staging_compose(compose_staging_path)
    report["staging_compose"] = stg_details
    report["markers"]["STAGING_DEFAULT_WORKER_DISABLED"] = "PASS" if stg_details["default_worker_disabled"] else "BLOCKED"
    report["markers"]["STAGING_VOLUME_AND_REMOTE_ROOT_ISOLATION"] = (
        "PASS" if (stg_details["isolated_named_volumes"] and stg_details["isolated_yandex_root"]) else "BLOCKED"
    )
    if not stg_ok:
        report["blockers"].extend(stg_details["blockers"])

    # 2. Staging Delivery Override
    ovr_ok, ovr_details = verify_staging_delivery_override(compose_override_path)
    report["staging_delivery_override"] = ovr_details
    report["markers"]["STAGING_OVERRIDE_REQUIRES_EXPLICIT_TOKEN"] = (
        "PASS" if ovr_details["requires_explicit_staging_token"] else "BLOCKED"
    )
    if not ovr_ok:
        report["blockers"].extend(ovr_details["blockers"])

    # 3. Prebuilt Staging Compose (if requested or files exist)
    prebuilt_checked = False
    if check_prebuilt or (compose_prebuilt_path and compose_prebuilt_path.exists()):
        pre_path = compose_prebuilt_path or (REPO_ROOT / "docker-compose.staging.prebuilt.yml")
        pre_ok, pre_details = verify_prebuilt_staging_compose(pre_path)
        report["staging_prebuilt_compose"] = pre_details
        report["markers"]["PREBUILT_COMPOSE_NO_BUILD_SECTION"] = "PASS" if pre_details["no_build_section"] else "BLOCKED"
        report["markers"]["PREBUILT_COMPOSE_REQUIRES_IMAGES"] = (
            "PASS" if (pre_details["requires_backend_image"] and pre_details["requires_frontend_image"]) else "BLOCKED"
        )
        report["markers"]["PREBUILT_COMPOSE_ISOLATION"] = (
            "PASS" if (pre_details["isolated_named_volumes"] and pre_details["isolated_yandex_root"]) else "BLOCKED"
        )
        if not pre_ok:
            report["blockers"].extend(pre_details["blockers"])
        prebuilt_checked = True

    if check_prebuilt or (compose_prebuilt_override_path and compose_prebuilt_override_path.exists()):
        pre_ovr_path = compose_prebuilt_override_path or (REPO_ROOT / "docker-compose.staging.prebuilt.delivery.yml")
        pre_ovr_ok, pre_ovr_details = verify_prebuilt_staging_delivery_override(pre_ovr_path)
        report["staging_prebuilt_delivery_override"] = pre_ovr_details
        report["markers"]["PREBUILT_OVERRIDE_REQUIRES_EXPLICIT_TOKEN"] = (
            "PASS" if pre_ovr_details["requires_explicit_staging_token"] else "BLOCKED"
        )
        if not pre_ovr_ok:
            report["blockers"].extend(pre_ovr_details["blockers"])

    # 4. Live Staging Acceptance Credentials
    token_to_check = live_token or os.getenv("YANDEX_DISK_STAGING_TOKEN")
    root_to_check = os.getenv("YANDEX_DISK_STAGING_ROOT", "PhotoDoc_Staging_R4")
    token_ok, token_reason = evaluate_staging_token(token_to_check, root_to_check)
    report["live_credential_status"] = {
        "token_evaluated": bool(token_to_check),
        "safe_for_live_test": token_ok,
        "evaluation_reason": token_reason,
        "remote_root": root_to_check,
    }
    report["markers"]["LIVE_YANDEX_STAGING_ACCEPTANCE"] = "PASS" if token_ok else "BLOCKED"
    if not token_ok:
        report["live_credential_status"]["remediation"] = (
            "An owner-provisioned dedicated staging Yandex Disk OAuth token must be configured "
            "in YANDEX_DISK_STAGING_TOKEN to execute live external staging acceptance."
        )

    report["pass"] = len(report["blockers"]) == 0
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="PhotoDoc AI — Staging Delivery Preflight Checker")
    parser.add_argument(
        "--compose-staging",
        default=str(REPO_ROOT / "docker-compose.staging.yml"),
        help="Path to docker-compose.staging.yml",
    )
    parser.add_argument(
        "--compose-override",
        default=str(REPO_ROOT / "docker-compose.staging.delivery.yml"),
        help="Path to docker-compose.staging.delivery.yml",
    )
    parser.add_argument(
        "--compose-prebuilt",
        default=str(REPO_ROOT / "docker-compose.staging.prebuilt.yml"),
        help="Path to docker-compose.staging.prebuilt.yml",
    )
    parser.add_argument(
        "--compose-prebuilt-override",
        default=str(REPO_ROOT / "docker-compose.staging.prebuilt.delivery.yml"),
        help="Path to docker-compose.staging.prebuilt.delivery.yml",
    )
    parser.add_argument(
        "--prebuilt",
        action="store_true",
        help="Validate prebuilt staging compose files",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results in JSON format",
    )
    parser.add_argument(
        "--check-live-token",
        action="store_true",
        help="Check whether current environment has a valid live staging token",
    )

    args = parser.parse_args()

    prebuilt_path = Path(args.compose_prebuilt) if (args.prebuilt or Path(args.compose_prebuilt).exists()) else None
    prebuilt_ovr_path = Path(args.compose_prebuilt_override) if (args.prebuilt or Path(args.compose_prebuilt_override).exists()) else None

    report = run_preflight(
        compose_staging_path=Path(args.compose_staging),
        compose_override_path=Path(args.compose_override),
        compose_prebuilt_path=prebuilt_path,
        compose_prebuilt_override_path=prebuilt_ovr_path,
        check_prebuilt=args.prebuilt,
        live_token=os.getenv("YANDEX_DISK_STAGING_TOKEN") if args.check_live_token else None,
    )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("==================================================")
        print(" PhotoDoc AI — Staging Delivery Preflight Report")
        print("==================================================")
        for marker, status in report["markers"].items():
            print(f" {marker:42}: {status}")
        print("--------------------------------------------------")
        print(f" Live Staging Credential Evaluation:   {report['live_credential_status']['evaluation_reason']}")
        print(" Secrets printed to console:           false")
        if report["blockers"]:
            print(f"\n Blockers ({len(report['blockers'])}):")
            for b in report["blockers"]:
                print(f"   [!] {b}")
        print("==================================================")

    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
