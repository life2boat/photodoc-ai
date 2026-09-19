#!/usr/bin/env python3
"""Build a deterministic, POSIX-safe PhotoDoc rollback tarball."""

from __future__ import annotations

import argparse
import gzip
import io
import os
import tarfile
import zipfile
from pathlib import Path

from rollback_bundle_common import (
    MANIFEST_NAME,
    forbidden_category,
    normalize_member_name,
    parse_manifest,
    render_manifest,
    sha256_bytes,
)


def collect_release_files(source: Path) -> list[tuple[str, Path]]:
    if not source.is_dir():
        raise ValueError(f"Source directory does not exist: {source}")
    files: list[tuple[str, Path]] = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        if path.is_symlink():
            raise ValueError(f"Symlinks are forbidden in rollback source: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"Non-regular rollback source entry: {relative}")
        if relative == MANIFEST_NAME:
            raise ValueError(f"Source must not contain generated {MANIFEST_NAME}")
        category = forbidden_category(relative)
        if category:
            raise ValueError(f"Forbidden {category}: {relative}")
        files.append((relative, path))
    if not files:
        raise ValueError("Rollback source contains no files")
    return files


def _tar_info(name: str, size: int, mode: int = 0o644) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.size = size
    info.mode = mode
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _write_bundle(
    content: list[tuple[str, bytes, int]], output: Path, manifest_output: Path
) -> dict[str, str | int]:
    manifest_items = [(name, sha256_bytes(data)) for name, data, _ in content]

    manifest = render_manifest(manifest_items)
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_output.parent.mkdir(parents=True, exist_ok=True)
    manifest_output.write_bytes(manifest)

    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for name, data, mode in content:
                    archive.addfile(_tar_info(name, len(data), mode), io.BytesIO(data))
                archive.addfile(
                    _tar_info(MANIFEST_NAME, len(manifest)), io.BytesIO(manifest)
                )

    return {
        "file_count": len(content),
        "bundle_sha256": sha256_bytes(output.read_bytes()),
    }


def build_bundle(source: Path, output: Path, manifest_output: Path) -> dict[str, str | int]:
    content: list[tuple[str, bytes, int]] = []
    for name, path in collect_release_files(source):
        mode = 0o755 if os.access(path, os.X_OK) else 0o644
        content.append((name, path.read_bytes(), mode))
    return _write_bundle(content, output, manifest_output)


def build_bundle_from_zip(
    source_zip: Path,
    selection_manifest: Path,
    output: Path,
    manifest_output: Path,
) -> dict[str, str | int]:
    """Normalize audited ZIP bytes selected by an independently captured manifest."""
    expected = parse_manifest(selection_manifest.read_bytes())
    members: dict[str, zipfile.ZipInfo] = {}
    with zipfile.ZipFile(source_zip) as archive:
        for info in archive.infolist():
            name = normalize_member_name(info.filename.rstrip("/"))
            if info.is_dir():
                continue
            if name in members:
                raise ValueError(f"Duplicate ZIP member: {name}")
            members[name] = info

        extras = set(members) - set(expected)
        for name in extras:
            category = forbidden_category(name)
            if category in {"secret-or-database", "secret-file", "credential-like-file"}:
                raise ValueError(f"Secret-bearing ZIP member cannot be normalized: {name}")
            if category not in {"runtime-state", "mutable-production-data"}:
                raise ValueError(f"ZIP contains an unaccounted release file: {name}")

        content: list[tuple[str, bytes, int]] = []
        for name, digest in sorted(expected.items()):
            if forbidden_category(name):
                raise ValueError(f"Selection manifest contains forbidden path: {name}")
            info = members.get(name)
            if info is None:
                raise ValueError(f"ZIP is missing production manifest file: {name}")
            data = archive.read(info)
            if sha256_bytes(data) != digest:
                raise ValueError(f"ZIP differs from production manifest: {name}")
            mode = 0o755 if ((info.external_attr >> 16) & 0o111) else 0o644
            content.append((name, data, mode))

    result = _write_bundle(content, output, manifest_output)
    result["omitted_mutable_files"] = len(extras)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", type=Path)
    source.add_argument("--source-zip", type=Path)
    parser.add_argument("--selection-manifest", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest-output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.source_zip:
            if not args.selection_manifest:
                parser.error("--selection-manifest is required with --source-zip")
            result = build_bundle_from_zip(
                args.source_zip,
                args.selection_manifest,
                args.output,
                args.manifest_output,
            )
        else:
            result = build_bundle(args.source, args.output, args.manifest_output)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"ROLLBACK_FILES={result['file_count']}")
    if "omitted_mutable_files" in result:
        print(f"OMITTED_MUTABLE_FILES={result['omitted_mutable_files']}")
    print("ROLLBACK_BUNDLE_SECRET_MATCHES=0")
    print("MUTABLE_PRODUCTION_DATA_INCLUDED=false")
    print(f"ROLLBACK_BUNDLE_SHA256={result['bundle_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
