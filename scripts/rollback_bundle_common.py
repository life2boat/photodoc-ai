"""Shared safety rules for PhotoDoc rollback bundles."""

from __future__ import annotations

import hashlib
import posixpath
import re
from pathlib import PurePosixPath
from typing import Iterable


MANIFEST_NAME = "manifest.sha256"
MUTABLE_COMPONENTS = {"uploads", "backups", "logs"}
FORBIDDEN_EXACT_FILES = {".env", "orders.db"}
RUNTIME_SUFFIXES = {".log", ".pid"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
FORBIDDEN_NAME_FRAGMENTS = {"token", "credential", "private_key", "id_rsa"}

PRIVATE_KEY_HEADER_RE = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY(?: BLOCK)?-----", re.IGNORECASE
)
SB_SECRET_RE = re.compile(r"\bsb_secret_[A-Za-z0-9._-]+")
SECRET_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?[\"']?(?P<name>[A-Za-z_][A-Za-z0-9_]*)[\"']?"
    r"[ \t]*[:=][ \t]*(?P<value>[^\r\n]*?)[ \t]*,?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
SECRET_NAME_FRAGMENTS = (
    "PASSWORD",
    "PASSWD",
    "TOKEN",
    "SECRET",
    "PRIVATE_KEY",
    "API_KEY",
    "CREDENTIAL",
)
PLACEHOLDER_VALUE_RE = re.compile(
    r"(?:"
    r"present|missing|none|null|false|true|0|1|"
    r"(?:ci|test|example|dummy|placeholder|changeme|change_me|redacted|masked|secret)"
    r"(?:[-_a-z0-9.]*)|"
    r"your[-_a-z0-9.]*|x+|\*+"
    r")",
    re.IGNORECASE,
)


def normalize_member_name(name: str) -> str:
    if "\\" in name:
        raise ValueError(f"Backslash archive path is forbidden: {name}")
    windows_absolute = len(name) >= 3 and name[1] == ":" and name[2] == "/"
    if (
        not name
        or name.startswith("/")
        or windows_absolute
        or PurePosixPath(name).is_absolute()
    ):
        raise ValueError(f"Absolute or empty archive path is forbidden: {name}")
    normalized = posixpath.normpath(name)
    if normalized in {".", ".."} or normalized.startswith("../"):
        raise ValueError(f"Path traversal is forbidden: {name}")
    if normalized != name.rstrip("/"):
        raise ValueError(f"Non-canonical archive path is forbidden: {name}")
    return normalized


def forbidden_category(name: str) -> str | None:
    normalized = normalize_member_name(name)
    parts = [part.lower() for part in PurePosixPath(normalized).parts]
    basename = parts[-1]
    if basename in FORBIDDEN_EXACT_FILES:
        return "secret-or-database"
    if any(part in MUTABLE_COMPONENTS for part in parts):
        return "mutable-production-data"
    if any(basename.endswith(suffix) for suffix in RUNTIME_SUFFIXES):
        return "runtime-state"
    if any(basename.endswith(suffix) for suffix in SECRET_SUFFIXES):
        return "secret-file"
    if any(fragment in basename for fragment in FORBIDDEN_NAME_FRAGMENTS):
        return "credential-like-file"
    return None


def content_secret_categories(data: bytes) -> set[str]:
    """Return secret categories only; never retain or report matched values."""
    text = data.decode("utf-8", errors="ignore")
    categories: set[str] = set()
    if PRIVATE_KEY_HEADER_RE.search(text):
        categories.add("private-key-header")
    if SB_SECRET_RE.search(text):
        categories.add("supabase-secret")

    for match in SECRET_ASSIGNMENT_RE.finditer(text):
        name = match.group("name").upper()
        if not any(fragment in name for fragment in SECRET_NAME_FRAGMENTS):
            continue
        value = match.group("value").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1].strip()
        if (
            not value
            or value.startswith(("${", "$", "{{", "<"))
            or (value.endswith(">") and value.startswith("<"))
            or value.startswith(("{", "[", "("))
            or re.match(r"[A-Za-z_][A-Za-z0-9_.]*\(", value)
            or PLACEHOLDER_VALUE_RE.fullmatch(value)
        ):
            continue
        categories.add("credential-assignment")
    return categories


def require_secret_free_content(name: str, data: bytes) -> None:
    """Reject embedded credential material without disclosing matched values."""
    categories = sorted(content_secret_categories(data))
    if categories:
        raise ValueError(
            f"Forbidden content-secret categories {','.join(categories)}: {name}"
        )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def render_manifest(items: Iterable[tuple[str, str]]) -> bytes:
    lines = [f"{digest}  {name}" for name, digest in sorted(items)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def parse_manifest(data: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in data.decode("utf-8").splitlines():
        if not raw_line:
            continue
        if len(raw_line) < 67 or raw_line[64:66] != "  ":
            raise ValueError("Malformed SHA256 manifest line")
        digest = raw_line[:64].lower()
        name = normalize_member_name(raw_line[66:])
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("Malformed SHA256 digest")
        if name == MANIFEST_NAME or name in result:
            raise ValueError(f"Invalid or duplicate manifest entry: {name}")
        result[name] = digest
    if not result:
        raise ValueError("Manifest is empty")
    return result
