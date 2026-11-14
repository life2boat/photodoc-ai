"""CI must collect the complete suite and keep unexpected skips visible."""
from pathlib import Path

import pytest
import yaml

from scripts.verify_backend_junit import verify


def test_full_backend_job_uses_entire_test_directory_and_release_fences():
    root = Path(__file__).resolve().parents[2]
    jobs = yaml.safe_load((root / ".github/workflows/playwright.yml").read_text())["jobs"]
    commands = "\n".join(step.get("run", "") for step in jobs["backend-full-tests"]["steps"])
    assert "python -m pytest tests -ra --junitxml=" in commands
    assert " -k " not in commands and "--ignore" not in commands
    assert "verify_backend_junit.py" in commands
    assert "false &&" in jobs["publish-images"]["if"]
    deploy = yaml.safe_load((root / ".github/workflows/deploy.yml").read_text())
    assert deploy["jobs"]["deploy"]["if"] == "${{ false }}"


@pytest.mark.parametrize("result", [
    '<skipped type="pytest.skip" message="symlink unavailable"/>',
    '<skipped type="pytest.xfail" message="known failure"/>',
    '<failure message="failed"/>', '<error message="collection failed"/>',
])
def test_junit_gate_rejects_hidden_or_failed_tests(tmp_path, result):
    report = tmp_path / "result.xml"
    report.write_text(f'<testsuite><testcase name="safety">{result}</testcase></testsuite>')
    with pytest.raises(ValueError):
        verify(report)


def test_junit_gate_reports_supported_docker_opt_in_skip(tmp_path):
    report = tmp_path / "result.xml"
    report.write_text('<testsuite><testcase classname="tests.test_pra_a1_receipts" '
                      'name="test_linux_docker_bind_mount_fd_and_uid">'
                      '<skipped type="pytest.skip" message="explicit isolated Linux Docker runtime gate"/>'
                      '</testcase></testsuite>')
    assert verify(report)["explicit_docker_opt_in_skips"] == ["test_linux_docker_bind_mount_fd_and_uid"]
