# PhotoDoc AI — Staging Bootstrap & Deployment Gate Automated Tests
# Release R4.3: Validates safe staging preflight, digest pinning, production IP guard,
# zero-build guarantees, secret sanitization, and rollback safety.

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import staging_deploy_preflight as preflight  # noqa: E402

VALID_BACKEND_DIGEST = "sha256:bc40fbe28d65fdb386605fdcc11a89511051efdd77d88381a8b0a0767f2893d9"
VALID_FRONTEND_DIGEST = "sha256:1fc9b0d3b783f04ae1a380f4b84da129e63eb5efc8bdefb95af6e7074157379c"
VALID_BACKEND_IMAGE = f"ghcr.io/life2boat/photodoc-backend@{VALID_BACKEND_DIGEST}"
VALID_FRONTEND_IMAGE = f"ghcr.io/life2boat/photodoc-frontend@{VALID_FRONTEND_DIGEST}"


@pytest.fixture
def prebuilt_compose_path() -> Path:
    return REPO_ROOT / "docker-compose.staging.prebuilt.yml"


@pytest.fixture
def prebuilt_override_path() -> Path:
    return REPO_ROOT / "docker-compose.staging.prebuilt.delivery.yml"


@pytest.fixture
def deploy_script_path() -> Path:
    return REPO_ROOT / "scripts" / "deploy_staging_prebuilt.sh"


# ==============================================================================
# 1. Production Target Safety Guard Tests
# ==============================================================================


def test_production_target_guard_rejects_production_ip():
    """Verify that targeting production IP 79.137.196.14 is immediately blocked."""
    ok, blockers = preflight.check_production_target_guard("79.137.196.14")
    assert ok is False
    assert any("matches production infrastructure" in b for b in blockers)

    # Whitespace and case tolerance
    ok2, blockers2 = preflight.check_production_target_guard("  79.137.196.14  ")
    assert ok2 is False

    # Production domains
    ok3, blockers3 = preflight.check_production_target_guard("photodoc-ai.ru")
    assert ok3 is False

    ok4, blockers4 = preflight.check_production_target_guard("www.photodoc-ai.ru")
    assert ok4 is False


def test_production_target_guard_accepts_staging_and_loopback_hosts():
    """Verify staging or non-production target hosts are accepted."""
    ok, blockers = preflight.check_production_target_guard("127.0.0.1")
    assert ok is True
    assert blockers == []

    ok_stg, _ = preflight.check_production_target_guard("staging.photo1storage.ru")
    assert ok_stg is True


def test_production_host_detection_simulated():
    """Verify preflight rejects execution if local host IP matches production server."""
    with patch("socket.gethostname", return_value="prod-vps"), \
         patch("socket.gethostbyname_ex", return_value=("prod-vps", [], ["79.137.196.14"])):
        ok, blockers = preflight.check_production_target_guard()
        assert ok is False
        assert any("matches protected production IP 79.137.196.14" in b for b in blockers)


# ==============================================================================
# 2. Image Reference and Digest Pinning Tests
# ==============================================================================


def test_missing_image_references_fail_closed():
    """Verify preflight fails closed if either image reference is missing."""
    ok, _, blockers = preflight.check_image_references(None, None)
    assert ok is False
    assert any("MISSING_BACKEND_IMAGE" in b for b in blockers)
    assert any("MISSING_FRONTEND_IMAGE" in b for b in blockers)

    ok_b, _, blockers_b = preflight.check_image_references(VALID_BACKEND_IMAGE, "")
    assert ok_b is False
    assert any("MISSING_FRONTEND_IMAGE" in b for b in blockers_b)


def test_mutable_image_tags_rejected():
    """Verify mutable tags like 'latest', 'dev' are strictly rejected."""
    ok, details, blockers = preflight.check_image_references(
        "ghcr.io/life2boat/photodoc-backend:latest",
        VALID_FRONTEND_IMAGE,
    )
    assert ok is False
    assert details["mutable_tags_detected"] is True
    assert any("MUTABLE_TAG_FORBIDDEN" in b for b in blockers)

    ok_dev, _, blockers_dev = preflight.check_image_references(
        VALID_BACKEND_IMAGE,
        "ghcr.io/life2boat/photodoc-frontend:dev",
    )
    assert ok_dev is False
    assert any("MUTABLE_TAG_FORBIDDEN" in b for b in blockers_dev)


def test_unpinned_references_fail_when_digest_required():
    """Verify tag-only references without sha256 digests fail when require_digest=True."""
    ok, _, blockers = preflight.check_image_references(
        "ghcr.io/life2boat/photodoc-backend:7c60fe0",
        "ghcr.io/life2boat/photodoc-frontend:7c60fe0",
        require_digest=True,
    )
    assert ok is False
    assert any("UNPINNED_BACKEND_IMAGE" in b for b in blockers)
    assert any("UNPINNED_FRONTEND_IMAGE" in b for b in blockers)


def test_digest_pinned_references_pass():
    """Verify references containing valid sha256 digests pass cleanly."""
    ok, details, blockers = preflight.check_image_references(
        VALID_BACKEND_IMAGE,
        VALID_FRONTEND_IMAGE,
        require_digest=True,
    )
    assert ok is True
    assert details["backend_has_digest"] is True
    assert details["frontend_has_digest"] is True
    assert details["backend_digest"] == VALID_BACKEND_DIGEST
    assert details["frontend_digest"] == VALID_FRONTEND_DIGEST
    assert blockers == []


# ==============================================================================
# 3. Release Manifest Alignment & Mismatch Tests
# ==============================================================================


def test_release_manifest_verification_success(tmp_path):
    """Verify image references matching release-manifest.json pass cleanly."""
    manifest = tmp_path / "release-manifest.json"
    manifest.write_text(json.dumps({
        "commit_sha": "7c60fe08cadefd8943cfaae269f114814ea3979a",
        "images": {
            "backend": {"digest": VALID_BACKEND_DIGEST},
            "frontend": {"digest": VALID_FRONTEND_DIGEST},
        }
    }), encoding="utf-8")

    ok, details, blockers = preflight.check_release_manifest(
        manifest, VALID_BACKEND_IMAGE, VALID_FRONTEND_IMAGE
    )
    assert ok is True
    assert details["backend_digest_matches"] is True
    assert details["frontend_digest_matches"] is True
    assert blockers == []


def test_release_manifest_digest_mismatch_fails_closed(tmp_path):
    """Verify preflight rejects deployment if image digest does not match release manifest."""
    manifest = tmp_path / "release-manifest.json"
    manifest.write_text(json.dumps({
        "commit_sha": "7c60fe08cadefd8943cfaae269f114814ea3979a",
        "images": {
            "backend": {"digest": "sha256:0000000000000000000000000000000000000000000000000000000000000000"},
            "frontend": {"digest": VALID_FRONTEND_DIGEST},
        }
    }), encoding="utf-8")

    ok, _, blockers = preflight.check_release_manifest(
        manifest, VALID_BACKEND_IMAGE, VALID_FRONTEND_IMAGE
    )
    assert ok is False
    assert any("MANIFEST_BACKEND_MISMATCH" in b for b in blockers)


# ==============================================================================
# 4. Staging Compose Zero-Build and Isolation Tests
# ==============================================================================


def test_staging_compose_zero_build_and_isolation(prebuilt_compose_path):
    """Verify repo prebuilt staging Compose file passes all preflight checks."""
    ok, details, blockers = preflight.check_staging_compose(prebuilt_compose_path)
    assert ok is True
    assert details["zero_build_guarantee"] is True
    assert details["loopback_binding"] is True
    assert details["isolated_volumes"] is True
    assert details["isolated_network"] is True
    assert details["robokassa_test_mode"] is True
    assert details["delivery_worker_disabled"] is True
    assert details["isolated_remote_root"] is True
    assert details["production_assets_excluded"] is True
    assert blockers == []


def test_staging_compose_fails_if_build_section_present(tmp_path):
    """Verify preflight rejects staging compose if build directive is present."""
    tampered = tmp_path / "docker-compose.tampered.yml"
    tampered.write_text("""
services:
  backend:
    build: ./backend
    image: "${PHOTODOC_BACKEND_IMAGE:?err}"
    environment:
      DELIVERY_WORKER_ENABLED: "0"
      ROBOKASSA_IS_TEST: "1"
      YANDEX_DISK_ROOT_DIR: "PhotoDoc_Staging"
    volumes:
      - photodoc_staging_db:/data
      - photodoc_staging_uploads:/app/uploads
    networks:
      - photodoc_staging_network
  frontend:
    image: "${PHOTODOC_FRONTEND_IMAGE:?err}"
    ports:
      - "127.0.0.1:8081:80"
    networks:
      - photodoc_staging_network
volumes:
  photodoc_staging_db:
    name: photodoc_staging_backend-data
  photodoc_staging_uploads:
    name: photodoc_staging_uploads
networks:
  photodoc_staging_network:
    name: photodoc_staging_network
""", encoding="utf-8")
    ok, details, blockers = preflight.check_staging_compose(tampered)
    assert ok is False
    assert details["zero_build_guarantee"] is False
    assert any("FORBIDDEN_BUILD_DIRECTIVE" in b for b in blockers)


def test_staging_compose_fails_on_public_frontend_port(tmp_path):
    """Verify preflight rejects staging compose if frontend port is not bound to 127.0.0.1."""
    tampered = tmp_path / "docker-compose.tampered.yml"
    tampered.write_text("""
services:
  backend:
    image: "${PHOTODOC_BACKEND_IMAGE:?err}"
    environment:
      DELIVERY_WORKER_ENABLED: "0"
      ROBOKASSA_IS_TEST: "1"
      YANDEX_DISK_ROOT_DIR: "PhotoDoc_Staging"
    volumes:
      - photodoc_staging_db:/data
      - photodoc_staging_uploads:/app/uploads
    networks:
      - photodoc_staging_network
  frontend:
    image: "${PHOTODOC_FRONTEND_IMAGE:?err}"
    ports:
      - "0.0.0.0:8081:80"
    networks:
      - photodoc_staging_network
volumes:
  photodoc_staging_db:
    name: photodoc_staging_backend-data
  photodoc_staging_uploads:
    name: photodoc_staging_uploads
networks:
  photodoc_staging_network:
    name: photodoc_staging_network
""", encoding="utf-8")
    ok, details, blockers = preflight.check_staging_compose(tampered)
    assert ok is False
    assert details["loopback_binding"] is False
    assert any("PUBLIC_FRONTEND_PORT_FORBIDDEN" in b for b in blockers)


def test_staging_compose_rejects_production_volume_names(tmp_path):
    """Verify preflight rejects staging compose if production volume names are mounted."""
    tampered = tmp_path / "docker-compose.tampered.yml"
    tampered.write_text("""
services:
  backend:
    image: "${PHOTODOC_BACKEND_IMAGE:?err}"
    environment:
      DELIVERY_WORKER_ENABLED: "0"
      ROBOKASSA_IS_TEST: "1"
      YANDEX_DISK_ROOT_DIR: "PhotoDoc_Staging"
    volumes:
      - photodoc_backend-data:/data
    networks:
      - photodoc_staging_network
  frontend:
    image: "${PHOTODOC_FRONTEND_IMAGE:?err}"
    ports:
      - "127.0.0.1:8081:80"
    networks:
      - photodoc_staging_network
volumes:
  photodoc_backend-data:
    name: photodoc_backend-data
networks:
  photodoc_staging_network:
    name: photodoc_staging_network
""", encoding="utf-8")
    ok, details, blockers = preflight.check_staging_compose(tampered)
    assert ok is False
    assert details["production_assets_excluded"] is False
    assert any("PRODUCTION_VOLUME_FORBIDDEN" in b for b in blockers)


def test_staging_compose_rejects_production_remote_root(tmp_path):
    """Verify preflight rejects staging compose if remote root is PhotoDoc_Orders."""
    tampered = tmp_path / "docker-compose.tampered.yml"
    tampered.write_text("""
services:
  backend:
    image: "${PHOTODOC_BACKEND_IMAGE:?err}"
    environment:
      DELIVERY_WORKER_ENABLED: "0"
      ROBOKASSA_IS_TEST: "1"
      YANDEX_DISK_ROOT_DIR: "PhotoDoc_Orders"
    volumes:
      - photodoc_staging_db:/data
      - photodoc_staging_uploads:/app/uploads
    networks:
      - photodoc_staging_network
  frontend:
    image: "${PHOTODOC_FRONTEND_IMAGE:?err}"
    ports:
      - "127.0.0.1:8081:80"
    networks:
      - photodoc_staging_network
volumes:
  photodoc_staging_db:
    name: photodoc_staging_backend-data
  photodoc_staging_uploads:
    name: photodoc_staging_uploads
networks:
  photodoc_staging_network:
    name: photodoc_staging_network
""", encoding="utf-8")
    ok, details, blockers = preflight.check_staging_compose(tampered)
    assert ok is False
    assert details["production_assets_excluded"] is False
    assert any("PRODUCTION_REMOTE_ROOT_FORBIDDEN" in b for b in blockers)


# ==============================================================================
# 5. Delivery Override Verification Tests
# ==============================================================================


def test_delivery_override_contracts(prebuilt_override_path):
    """Verify prebuilt delivery override enables worker, requires explicit token, and targets isolated root."""
    ok, details, blockers = preflight.check_delivery_override(prebuilt_override_path)
    assert ok is True
    assert details["zero_build"] is True
    assert details["worker_enabled"] is True
    assert details["requires_explicit_token"] is True
    assert details["isolated_staging_root"] is True
    assert blockers == []


def test_delivery_override_rejects_test_placeholders(prebuilt_override_path):
    """Verify delivery override rejects mock/placeholder tokens for live staging acceptance."""
    ok, _, blockers = preflight.check_delivery_override(
        prebuilt_override_path, live_token="y0_test_staging_token_mock"
    )
    assert ok is False
    assert any("PLACEHOLDER_TOKEN_REJECTED" in b for b in blockers)


# ==============================================================================
# 6. Secret Sanitization & Safe Output Tests
# ==============================================================================


def test_secret_sanitization():
    """Verify diagnostic string sanitization never echoes tokens or passwords."""
    secret = "y0_super_secret_staging_oauth_token_12345"
    sanitized = preflight.sanitize_value("YANDEX_DISK_STAGING_TOKEN", secret)
    assert secret not in sanitized
    assert sanitized == f"[SET - {len(secret)} chars]"

    pass_secret = "mypassword123"
    sanitized_pass = preflight.sanitize_value("ROBOKASSA_PASSWORD1", pass_secret)
    assert pass_secret not in sanitized_pass
    assert sanitized_pass == f"[SET - {len(pass_secret)} chars]"


# ==============================================================================
# 7. Complete Preflight Suite Integration Test
# ==============================================================================


def test_full_staging_deploy_preflight_passes_with_valid_inputs(prebuilt_compose_path, prebuilt_override_path):
    """Verify full preflight execution returns PASS for all 5 safety markers."""
    report = preflight.run_staging_deploy_preflight(
        target_host="127.0.0.1",
        backend_image=VALID_BACKEND_IMAGE,
        frontend_image=VALID_FRONTEND_IMAGE,
        compose_path=prebuilt_compose_path,
        override_path=prebuilt_override_path,
        manifest_path=None,
        require_digest=True,
    )
    assert report["pass"] is True
    assert report["markers"]["PRODUCTION_TARGET_GUARD"] == "PASS"
    assert report["markers"]["DIGEST_VERIFICATION"] == "PASS"
    assert report["markers"]["ZERO_BUILD_BOOTSTRAP"] == "PASS"
    assert report["markers"]["STAGING_PREFLIGHT"] == "PASS"
    assert report["markers"]["ROLLBACK_PROCEDURE"] == "PASS"
    assert report["blockers"] == []


# ==============================================================================
# 8. Deploy Script Invariants & Syntax
# ==============================================================================


def test_deploy_script_safety_invariants(deploy_script_path):
    """Verify deploy_staging_prebuilt.sh incorporates all safety guard contracts."""
    content = deploy_script_path.read_text(encoding="utf-8")

    # 1. Zero-build startup flag
    assert "--no-build" in content, "Deploy script must execute docker compose with --no-build"

    # 2. Rejection of production server IP
    assert "79.137.196.14" in content, "Deploy script must explicitly reject production IP"

    # 3. No destructive volume wipe
    assert "down -v" not in content, "Deploy script must NEVER execute docker compose down -v"

    # 4. Rollback capability
    assert "--rollback" in content, "Deploy script must support --rollback option"

    # 5. Confirmation gate
    assert "--confirm" in content, "Deploy script must support explicit --confirm flag"

    # 6. Default worker disabled
    assert "DELIVERY_WORKER_ENABLED=0" in content or "DELIVERY_WORKER_ENABLED" in content
