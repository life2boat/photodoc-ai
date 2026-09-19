"""Shared safety rules for PhotoDoc rollback bundles."""

from __future__ import annotations

import hashlib
import posixpath
from pathlib import PurePosixPath
from typing import Iterable


MANIFEST_NAME = "manifest.sha256"
MUTABLE_COMPONENTS = {"uploads", "backups", "logs"}
FORBIDDEN_EXACT_FILES = {".env", "orders.db"}
RUNTIME_SUFFIXES = {".log", ".pid"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
FORBIDDEN_NAME_FRAGMENTS = {"token", "credential", "private_key", "id_rsa"}


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
