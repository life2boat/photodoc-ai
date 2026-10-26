#!/usr/bin/env python3
"""Verify and optionally rehearse extraction of a PhotoDoc rollback bundle."""

from __future__ import annotations

import argparse
import tarfile
from pathlib import Path

from rollback_bundle_common import (
    MANIFEST_NAME,
    forbidden_category,
    normalize_member_name,
    parse_manifest,
    require_secret_free_content,
    sha256_bytes,
)


def verify_bundle(bundle: Path, *, required: list[str] | None = None) -> dict[str, bytes]:
    if not bundle.is_file():
        raise ValueError(f"Bundle not found: {bundle}")
    contents: dict[str, bytes] = {}
    with tarfile.open(bundle, mode="r:gz") as archive:
        for member in archive.getmembers():
            name = normalize_member_name(member.name)
            if member.issym() or member.islnk() or member.isdev():
                raise ValueError(f"Unsafe archive member type: {name}")
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError(f"Unsupported archive member type: {name}")
            if name in contents:
                raise ValueError(f"Duplicate archive member: {name}")
            category = None if name == MANIFEST_NAME else forbidden_category(name)
            if category:
                raise ValueError(f"Forbidden {category}: {name}")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValueError(f"Unable to read archive member: {name}")
            data = extracted.read()
            if name != MANIFEST_NAME:
                require_secret_free_content(name, data)
            contents[name] = data

    if MANIFEST_NAME not in contents:
        raise ValueError(f"Bundle is missing {MANIFEST_NAME}")
    manifest = parse_manifest(contents[MANIFEST_NAME])
    actual_names = set(contents) - {MANIFEST_NAME}
    if actual_names != set(manifest):
        raise ValueError("Bundle contents do not exactly match manifest entries")
    for name, expected in manifest.items():
        if sha256_bytes(contents[name]) != expected:
            raise ValueError(f"Manifest hash mismatch: {name}")
    for name in required or []:
        normalized = normalize_member_name(name)
        if normalized not in actual_names:
            raise ValueError(f"Required release file is missing: {normalized}")
    return contents


def rehearse_extract(contents: dict[str, bytes], target: Path) -> None:
    if target.exists() and any(target.iterdir()):
        raise ValueError(f"Extraction target must be empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    for name, data in sorted(contents.items()):
        destination = (root / Path(*name.split("/"))).resolve()
        if root not in destination.parents:
            raise ValueError(f"Extraction escaped target: {name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--extract-to", type=Path)
    parser.add_argument("--require", action="append", default=[])
    args = parser.parse_args()
    try:
        contents = verify_bundle(args.bundle, required=args.require)
        if args.extract_to:
            rehearse_extract(contents, args.extract_to)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
    print("ROLLBACK_CONTENT_SECRET_SCAN=true")
    print("ROLLBACK_SECRET_VALUES_PRINTED=false")
    print("ROLLBACK_BUNDLE_SECRET_MATCHES=0")
    print("FORBIDDEN_PATH_MATCHES=0")
    print("MUTABLE_PRODUCTION_DATA_INCLUDED=false")
    print("MANIFEST_HASH_VERIFICATION=PASS")
    if args.extract_to:
        print("LINUX_EXTRACTION_REHEARSAL=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
