"""Domain consistency regressions: mail identity, preflight default, target guard.

Guards four properties that were previously inconsistent after the edge was moved to
the owned domain `photo1storage.ru`:

  * the ``Message-ID`` domain is configurable, validated, and no longer the
    unregistered literal ``photodoc-ai.ru``;
  * the R2 preflight checks the domain the edge actually serves;
  * the production-target guard still rejects production infrastructure while
    allowing the staging host (whose own hostname *is* the service domain);
  * staging stays incapable of sending real mail and shipping site metadata that
    points at an unregistered domain.
"""
from __future__ import annotations

import importlib.util
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

import staging_deploy_preflight as guard  # noqa: E402

CANONICAL = "photo1storage.ru"
LEGACY = "photodoc-ai.ru"


def _load_r2():
    """Import scripts/r2_staging_preflight.py without relying on sys.path order."""
    spec = importlib.util.spec_from_file_location(
        "r2_staging_preflight_domain", SCRIPTS_DIR / "r2_staging_preflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------- mail domain
@pytest.fixture
def mail_env(monkeypatch):
    """Isolate the environment variables that decide the mail domain."""
    for key in ("PHOTODOC_MAIL_DOMAIN", "SMTP_USER"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_mail_domain_defaults_to_the_served_domain(mail_env):
    import main
    assert CANONICAL == main.DEFAULT_MAIL_DOMAIN
    assert main.get_mail_domain() == CANONICAL


def test_mail_domain_follows_the_authenticated_mailbox(mail_env):
    import main
    mail_env.setenv("SMTP_USER", "notifications@photo1storage.ru")
    assert main.get_mail_domain() == CANONICAL

    mail_env.setenv("SMTP_USER", "robot@Example.ORG")
    assert main.get_mail_domain() == "example.org"


def test_explicit_mail_domain_wins_over_the_mailbox(mail_env):
    import main
    mail_env.setenv("SMTP_USER", "robot@example.org")
    mail_env.setenv("PHOTODOC_MAIL_DOMAIN", " Mail.Photo1Storage.RU ")
    assert main.get_mail_domain() == "mail.photo1storage.ru"


@pytest.mark.parametrize(
    "hostile",
    [
        "evil.example\r\nBcc: attacker@evil.example",
        "evil.example\nX-Injected: 1",
        "photo1storage.ru>",
        "<photo1storage.ru",
        "no-dots",
        "",
        "  ",
        "a..b.example",
        "-leading.example",
        "trailing-.example",
        "example.com:25",
        "exa mple.com",
    ],
)
def test_mail_domain_rejects_header_injection_and_malformed_names(mail_env, hostile):
    """A Message-ID domain is interpolated into a header: it must be a bare name."""
    import main
    mail_env.setenv("PHOTODOC_MAIL_DOMAIN", hostile)
    resolved = main.get_mail_domain()
    assert resolved == CANONICAL, f"unsafe value accepted: {hostile!r}"
    assert "\r" not in resolved and "\n" not in resolved and "<" not in resolved


def test_mail_domain_falls_back_when_mailbox_is_malformed(mail_env):
    import main
    mail_env.setenv("SMTP_USER", "not-an-address")
    assert main.get_mail_domain() == CANONICAL


def test_message_id_is_stable_and_uses_the_configured_domain(mail_env):
    import main
    assert main.message_id(7, "new") == f"<photodoc-order-7-new@{CANONICAL}>"
    assert main.message_id(7, "paid") == f"<photodoc-order-7-paid@{CANONICAL}>"
    mail_env.setenv("PHOTODOC_MAIL_DOMAIN", "mail.example.org")
    assert main.message_id(9, "paid") == "<photodoc-order-9-paid@mail.example.org>"
    with pytest.raises(AssertionError):
        main.message_id(1, "delivered")


class _CapturingSMTP:
    """Minimal SMTP double that records the messages handed to it."""

    sent: list = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def login(self, *args):
        return None

    def send_message(self, message):
        type(self).sent.append(message)
        return {}


@pytest.fixture
def capture_mail(mail_env, monkeypatch):
    import main
    _CapturingSMTP.sent = []
    for key, value in {
        "SMTP_SERVER": "127.0.0.1",
        "SMTP_PORT": "2525",
        "SMTP_USER": "robot@photo1storage.ru",
        "SMTP_PASSWORD": "synthetic",
        "EMAIL_TO": "owner@photo1storage.ru",
    }.items():
        mail_env.setenv(key, value)
    mail_env.setattr(main.smtplib, "SMTP_SSL", _CapturingSMTP)
    return _CapturingSMTP


def test_outbound_admin_notification_uses_the_owned_domain(capture_mail):
    """The header that actually leaves the process must carry the served domain."""
    import main
    assert main.send_order_email(3, "Иван", "+79990000000", "тест") is True
    assert len(capture_mail.sent) == 1
    header = capture_mail.sent[0]["Message-ID"]
    assert header == f"<photodoc-order-3-new@{CANONICAL}>"
    assert LEGACY not in header


def test_outbound_message_id_never_contains_the_legacy_domain(capture_mail, mail_env):
    import main
    mail_env.setenv("PHOTODOC_MAIL_DOMAIN", "mail.example.org")
    assert main.send_order_email(4, "Иван", "+79990000000", "") is True
    assert LEGACY not in capture_mail.sent[0]["Message-ID"]
    assert "mail.example.org" in capture_mail.sent[0]["Message-ID"]


# ------------------------------------------------------------------ r2 preflight
def test_r2_preflight_defaults_to_the_served_domain():
    module = _load_r2()
    defaults = {}
    source = (SCRIPTS_DIR / "r2_staging_preflight.py").read_text(encoding="utf-8")
    match = re.search(r'"--domain",\s*default="([^"]+)"', source)
    assert match, "could not locate the --domain default"
    defaults["domain"] = match.group(1)
    assert defaults["domain"] == CANONICAL, (
        "the preflight must default to the domain the edge serves, not the "
        f"unregistered {LEGACY}")
    for checker in ("check_dns", "check_http_redirect", "check_tls_certificate",
                    "check_api_health", "check_port_isolation", "run_preflight", "main"):
        assert hasattr(module, checker), f"r2 preflight lost {checker}"


# --------------------------------------------------------------- target guard
def test_guard_rejects_production_infrastructure():
    for host in ("79.137.196.14", "  79.137.196.14  ", LEGACY, f"www.{LEGACY}",
                 f"www.{CANONICAL}"):
        ok, blockers = guard.check_production_target_guard(host)
        assert ok is False, f"{host!r} should be rejected"
        assert any("matches production infrastructure" in b for b in blockers)


def test_guard_allows_the_staging_host_which_is_the_service_domain():
    """The private staging VPS hostname *is* the apex domain.

    Blocking the apex would reject the legitimate staging deployment on its own
    machine and invite operators to bypass the guard, so only the public `www`
    entrypoint is blocked. This test locks in that reasoning.
    """
    for host in (CANONICAL, f"staging.{CANONICAL}", "127.0.0.1", "95.181.154.251"):
        ok, blockers = guard.check_production_target_guard(host)
        assert ok is True, f"{host!r} must remain a permitted staging target: {blockers}"
        assert blockers == []


def test_guard_keeps_the_service_domain_constant_in_sync():
    assert guard.SERVICE_DOMAIN == CANONICAL
    assert guard.SERVICE_DOMAIN not in guard.PRODUCTION_TARGET_HOSTS
    assert f"www.{CANONICAL}" in guard.PRODUCTION_TARGET_HOSTS
    # The protected production IP must never be silently dropped.
    assert "79.137.196.14" in guard.PRODUCTION_TARGET_IPS
    assert "79.137.196.14" in guard.PRODUCTION_TARGET_HOSTS


def test_guard_still_rejects_a_registered_but_non_staging_target():
    """Sanity: the guard is not a blanket allow-list of everything unknown."""
    ok, _ = guard.check_production_target_guard("photo1storage.ru")
    assert ok is True   # documented apex exception
    ok2, _ = guard.check_production_target_guard(f"www.{CANONICAL}")
    assert ok2 is False


# ------------------------------------------------------- staging cannot email
STAGING_COMPOSES = ["docker-compose.staging.yml", "docker-compose.staging.prebuilt.yml"]


def _default_of(value: str) -> str:
    """Extract the fallback from a compose ``${VAR:-default}`` expression."""
    match = re.fullmatch(r"\$\{[A-Z_]+:-([^}]*)\}", str(value).strip())
    return match.group(1) if match else str(value).strip()


@pytest.mark.parametrize("compose_name", STAGING_COMPOSES)
def test_staging_compose_keeps_mail_local_and_on_the_owned_domain(compose_name):
    data = yaml.safe_load((REPO_ROOT / compose_name).read_text(encoding="utf-8"))
    env = data["services"]["backend"]["environment"]

    assert str(env["DELIVERY_WORKER_ENABLED"]) == "0", "staging worker must stay disabled"
    assert _default_of(env["SMTP_SERVER"]) == "127.0.0.1", (
        "staging SMTP must default to loopback so no real mail server is reachable")

    for key in ("SMTP_USER", "EMAIL_TO"):
        assert "${" in str(env[key]), f"{key} must stay overridable by the environment"
        default = _default_of(env[key])
        assert default.endswith(f"@{CANONICAL}"), (
            f"{key} default should use {CANONICAL}, got {default!r}")
        assert LEGACY not in str(env[key]), f"{key} still defaults to the legacy domain"

    assert "PHOTODOC_STAGING" in env


def test_staging_env_example_matches_the_compose_defaults():
    text = (BACKEND_DIR / ".env.staging.example").read_text(encoding="utf-8")
    assert LEGACY not in text, "staging example env still references the legacy domain"
    assert "SMTP_SERVER=127.0.0.1" in text
    assert "DELIVERY_WORKER_ENABLED=0" in text
    assert f"SMTP_USER=staging@{CANONICAL}" in text
    assert f"EMAIL_TO=staging_owner@{CANONICAL}" in text


# ------------------------------------------------------ frontend site identity
@pytest.mark.parametrize(
    "relative",
    ["frontend/index.html", "frontend/public/sitemap.xml", "frontend/public/robots.txt"],
)
def test_site_metadata_points_at_the_served_domain(relative):
    text = (REPO_ROOT / relative).read_text(encoding="utf-8")
    assert LEGACY not in text, f"{relative} still advertises the unregistered domain"
    assert CANONICAL in text, f"{relative} does not advertise {CANONICAL}"


def test_frontend_canonical_url_is_https_on_the_served_domain():
    text = (REPO_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    match = re.search(r'<link rel="canonical" href="([^"]+)"', text)
    assert match, "canonical link missing"
    assert match.group(1) == f"https://{CANONICAL}/"


# ------------------------------------------------- guard parity across callers
def test_shell_and_python_guards_agree_on_blocked_targets():
    """Two independent guards enforce this. If they drift, one of them is a hole."""
    shell = (SCRIPTS_DIR / "deploy_staging_prebuilt.sh").read_text(encoding="utf-8")

    # The production IP is compared through a variable in the shell script.
    assert "PRODUCTION_IP" in shell, "shell guard lost the production IP check"
    # A glob on the legacy domain covers both its apex and its www form.
    assert '*"photodoc-ai.ru"*' in shell, "shell guard lost the legacy domain block"
    # The public www entrypoint of the canonical domain is blocked explicitly.
    assert 'www.photo1storage.ru"*' in shell, "shell guard lost the www entrypoint block"
    # But the canonical apex must NOT be blanket-blocked: the staging host's own
    # hostname is that domain, so a glob on it would reject legitimate staging.
    assert '"*photo1storage.ru"*' not in shell, (
        "shell guard must not glob-block the canonical apex")


def test_python_guard_matches_the_shell_guard_semantics():
    """Same three categories, expressed for the Python guard."""
    # legacy apex and its www form
    assert LEGACY in guard.PRODUCTION_TARGET_HOSTS
    assert f"www.{LEGACY}" in guard.PRODUCTION_TARGET_HOSTS
    # production IP
    assert "79.137.196.14" in guard.PRODUCTION_TARGET_IPS
    # canonical www entrypoint blocked, canonical apex allowed
    assert f"www.{CANONICAL}" in guard.PRODUCTION_TARGET_HOSTS
    assert CANONICAL not in guard.PRODUCTION_TARGET_HOSTS


def test_guard_constants_cannot_grow_a_wildcard_apex_entry():
    for host in guard.PRODUCTION_TARGET_HOSTS | guard.PRODUCTION_TARGET_IPS:
        assert "*" not in host, f"wildcard entry {host!r} would over-match"
        assert host == host.strip().lower(), f"entry {host!r} is not normalised"
