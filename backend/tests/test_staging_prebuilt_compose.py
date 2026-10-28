# PhotoDoc AI — Staging Prebuilt Docker Compose & GHCR Publishing Tests
# Validates prebuilt staging compose files, zero local build requirements,
# image fail-closed contracts, staging isolation, and GitHub Actions publishing security.

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest
import yaml

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import verify_staging_delivery_preflight as staging_preflight  # noqa: E402


@pytest.fixture
def prebuilt_compose_path() -> Path:
    return REPO_ROOT / "docker-compose.staging.prebuilt.yml"


@pytest.fixture
def prebuilt_override_path() -> Path:
    return REPO_ROOT / "docker-compose.staging.prebuilt.delivery.yml"


@pytest.fixture
def workflow_path() -> Path:
    return REPO_ROOT / ".github" / "workflows" / "playwright.yml"


# ==============================================================================
# 1. Structure and Zero-Build Verification
# ==============================================================================


def test_prebuilt_compose_files_exist(prebuilt_compose_path, prebuilt_override_path):
    assert prebuilt_compose_path.is_file(), "docker-compose.staging.prebuilt.yml must exist"
    assert prebuilt_override_path.is_file(), "docker-compose.staging.prebuilt.delivery.yml must exist"


def test_prebuilt_compose_has_no_build_sections(prebuilt_compose_path, prebuilt_override_path):
    """Ensure running prebuilt compose will NEVER trigger a local compilation on low-memory VPS."""
    for path in (prebuilt_compose_path, prebuilt_override_path):
        content = path.read_text(encoding="utf-8")
        assert not re.search(
            r"^\s*build\s*:", content, re.MULTILINE
        ), f"Found forbidden 'build:' section in {path.name}"

        parsed = yaml.safe_load(content)
        services = parsed.get("services", {})
        for s_name, s_conf in services.items():
            if isinstance(s_conf, dict):
                assert "build" not in s_conf, f"Service {s_name} in {path.name} contains 'build' key"


def test_prebuilt_compose_requires_image_environment_variables(prebuilt_compose_path):
    """Verify that backend and frontend services mandate external image tags with fail-closed :? syntax."""
    content = prebuilt_compose_path.read_text(encoding="utf-8")
    assert "${PHOTODOC_BACKEND_IMAGE:?" in content, (
        "Backend image must enforce ${PHOTODOC_BACKEND_IMAGE:?...} fail-closed variable"
    )
    assert "${PHOTODOC_FRONTEND_IMAGE:?" in content, (
        "Frontend image must enforce ${PHOTODOC_FRONTEND_IMAGE:?...} fail-closed variable"
    )


# ==============================================================================
# 2. Isolation and Security Contracts
# ==============================================================================


def test_prebuilt_compose_isolation_contracts(prebuilt_compose_path):
    """Verify staging isolation: loopback binding, named volumes, staging network, disabled worker."""
    content = prebuilt_compose_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)

    backend = parsed["services"]["backend"]
    frontend = parsed["services"]["frontend"]

    # 1. Frontend bound to loopback 127.0.0.1
    ports = frontend.get("ports", [])
    assert any("127.0.0.1:" in str(p) for p in ports), f"Frontend must bind to 127.0.0.1, got {ports}"

    # 2. Named staging volumes
    volumes = parsed.get("volumes", {})
    assert "photodoc_staging_db" in volumes
    assert volumes["photodoc_staging_db"]["name"] == "photodoc_staging_backend-data"
    assert "photodoc_staging_uploads" in volumes
    assert volumes["photodoc_staging_uploads"]["name"] == "photodoc_staging_uploads"

    # 3. Production volumes strictly excluded
    assert "photodoc_backend-data\n" not in content
    assert "photodoc_uploads\n" not in content

    # 4. Delivery worker disabled by default
    backend_env = backend.get("environment", {})
    assert str(backend_env.get("DELIVERY_WORKER_ENABLED")) == "0"

    # 5. Robokassa test mode
    assert str(backend_env.get("ROBOKASSA_IS_TEST")) == "1"

    # 6. Isolated Yandex Disk root
    root_dir = str(backend_env.get("YANDEX_DISK_ROOT_DIR", ""))
    assert "PhotoDoc_Staging" in root_dir
    assert "PhotoDoc_Orders" not in root_dir

    # 7. Staging database identity flags
    assert str(backend_env.get("PHOTODOC_STAGING")) == "1"
    assert str(backend_env.get("PHOTODOC_REQUIRE_DB_IDENTITY")) == "1"
    assert str(backend_env.get("PHOTODOC_ALLOW_FRESH_DB")) == "1"


def test_prebuilt_delivery_override_contracts(prebuilt_override_path):
    """Verify prebuilt delivery override enables worker, requires explicit token, and isolates destination."""
    content = prebuilt_override_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)

    backend = parsed["services"]["backend"]
    env = backend.get("environment", {})

    # 1. Enables worker
    assert str(env.get("DELIVERY_WORKER_ENABLED")) == "1"

    # 2. Requires explicit staging token with fail-closed :? modifier
    assert "${YANDEX_DISK_STAGING_TOKEN:?" in content

    # 3. Dedicated staging root PhotoDoc_Staging_R4
    root = str(env.get("YANDEX_DISK_ROOT_DIR", ""))
    assert "PhotoDoc_Staging_R4" in root
    assert "PhotoDoc_Orders" not in root

    # 4. No production mounts or env files
    assert "env_file" not in backend
    assert "volumes" not in backend


# ==============================================================================
# 3. Preflight Script Integration
# ==============================================================================


def test_preflight_script_validates_prebuilt_compose(prebuilt_compose_path, prebuilt_override_path):
    """Verify scripts/verify_staging_delivery_preflight.py properly passes valid prebuilt compose files."""
    ok_comp, details_comp = staging_preflight.verify_prebuilt_staging_compose(prebuilt_compose_path)
    assert ok_comp is True
    assert details_comp["no_build_section"] is True
    assert details_comp["requires_backend_image"] is True
    assert details_comp["requires_frontend_image"] is True
    assert details_comp["default_worker_disabled"] is True
    assert details_comp["isolated_named_volumes"] is True
    assert details_comp["blockers"] == []

    ok_ovr, details_ovr = staging_preflight.verify_prebuilt_staging_delivery_override(prebuilt_override_path)
    assert ok_ovr is True
    assert details_ovr["no_build_section"] is True
    assert details_ovr["worker_enabled_in_override"] is True
    assert details_ovr["requires_explicit_staging_token"] is True
    assert details_ovr["blockers"] == []


def test_preflight_script_fails_closed_on_tampered_prebuilt_compose(tmp_path):
    """Verify preflight rejects prebuilt compose if a build block is added or images are hardcoded without fail-closed."""
    tampered = tmp_path / "docker-compose.tampered.yml"

    # Test 1: Contains build block
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
  frontend:
    image: "${PHOTODOC_FRONTEND_IMAGE:?err}"
    ports:
      - "127.0.0.1:8081:80"
volumes:
  photodoc_staging_db:
    name: photodoc_staging_backend-data
  photodoc_staging_uploads:
    name: photodoc_staging_uploads
""", encoding="utf-8")
    ok, details = staging_preflight.verify_prebuilt_staging_compose(tampered)
    assert ok is False
    assert any("build" in b for b in details["blockers"])

    # Test 2: Missing fail-closed variable
    tampered.write_text("""
services:
  backend:
    image: "photodoc-backend:latest"
    environment:
      DELIVERY_WORKER_ENABLED: "0"
      ROBOKASSA_IS_TEST: "1"
      YANDEX_DISK_ROOT_DIR: "PhotoDoc_Staging"
    volumes:
      - photodoc_staging_db:/data
      - photodoc_staging_uploads:/app/uploads
  frontend:
    image: "${PHOTODOC_FRONTEND_IMAGE:?err}"
    ports:
      - "127.0.0.1:8081:80"
volumes:
  photodoc_staging_db:
    name: photodoc_staging_backend-data
  photodoc_staging_uploads:
    name: photodoc_staging_uploads
""", encoding="utf-8")
    ok, details = staging_preflight.verify_prebuilt_staging_compose(tampered)
    assert ok is False
    assert any("PHOTODOC_BACKEND_IMAGE" in b for b in details["blockers"])


# ==============================================================================
# 4. GitHub Actions Workflow Architecture Tests
# ==============================================================================


def test_workflow_publishing_permissions_and_scoping(workflow_path):
    """Verify playwright.yml grants packages:write ONLY to publish-images job on trusted push."""
    content = workflow_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)

    jobs = parsed.get("jobs", {})
    assert "docker-build" in jobs
    assert "playwright-tests" in jobs
    assert "payment-security-tests" in jobs
    assert "publish-images" in jobs

    # 1. Global permissions must NOT have packages: write
    global_perms = parsed.get("permissions", {})
    if isinstance(global_perms, dict):
        assert global_perms.get("packages") != "write", "Global permissions must not have packages: write"

    # 2. PR testing jobs must NOT have packages: write
    for test_job in ("playwright-tests", "payment-security-tests", "docker-build"):
        job_perms = jobs[test_job].get("permissions", {})
        if isinstance(job_perms, dict):
            assert job_perms.get("packages") != "write", f"Job {test_job} must not have packages: write"

    # 3. publish-images job has packages: write
    pub_perms = jobs["publish-images"].get("permissions", {})
    assert pub_perms.get("packages") == "write", "publish-images job must have packages: write"

    # 4. publish-images depends on all tests passing
    needs = jobs["publish-images"].get("needs", [])
    assert set(needs) >= {"docker-build", "payment-security-tests", "playwright-tests"}

    # 5. Trusted branch restriction on publish-images
    job_condition = str(jobs["publish-images"].get("if", ""))
    assert "github.event_name == 'push'" in job_condition or "push" in job_condition
    assert "infra/r4-prebuilt-staging-images" in job_condition
    assert "main" in job_condition


def test_workflow_image_tagging_and_oci_labels(workflow_path):
    """Verify images are tagged with full git commit SHA and have standard OCI labels."""
    content = workflow_path.read_text(encoding="utf-8")

    # Images must be tagged with commit SHA
    assert "${{ github.sha }}" in content
    # OCI labels
    assert "org.opencontainers.image.source" in content
    assert "org.opencontainers.image.revision" in content
    assert "org.opencontainers.image.created" in content

    # Manifest generation
    assert "release-manifest.json" in content
    assert "GITHUB_STEP_SUMMARY" in content
