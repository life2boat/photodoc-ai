"""Explicit host-owner setup and verification. Never called by socket clients."""

from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .receipt_ledger import ReceiptLedger, sync_directory
from .receipt_protocol import ReceiptError, UUID4
from .receipt_service import from_config


def generate_key(private_path: Path, public_path: Path) -> None:
    """Exclusive publication of a new Ed25519 keypair; no overwrite."""
    private_path = Path(private_path)
    public_path = Path(public_path)
    if private_path.exists() or public_path.exists():
        raise ReceiptError("CONFLICT", "key path exists")
    key = Ed25519PrivateKey.generate()
    private_bytes = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_bytes = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    try:
        for path, data, mode in (
            (private_path, private_bytes, 0o400), (public_path, public_bytes, 0o444),
        ):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(path, mode)
        sync_directory(private_path.parent)
        if public_path.parent != private_path.parent:
            sync_directory(public_path.parent)
    except Exception as exc:
        # Do not remove a possibly durable private key automatically.
        raise ReceiptError("IO_UNCERTAIN", "key publication incomplete; inspect created paths") from exc


def _read_private(path: Path) -> Ed25519PrivateKey:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ReceiptError("INVALID", "private key permissions")
    return Ed25519PrivateKey.from_private_bytes(path.read_bytes())


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    key = commands.add_parser("generate-key")
    key.add_argument("--private", type=Path, required=True)
    key.add_argument("--public", type=Path, required=True)
    init = commands.add_parser("init-store")
    init.add_argument("--root", type=Path, required=True)
    init.add_argument("--instance-uuid", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--config", type=Path, required=True)
    rotate = commands.add_parser("rotate-key")
    rotate.add_argument("--config", type=Path, required=True)
    rotate.add_argument("--new-key-id", required=True)
    rotate.add_argument("--new-private", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "generate-key":
        generate_key(args.private, args.public)
    elif args.command == "init-store":
        if not UUID4.fullmatch(args.instance_uuid):
            raise ReceiptError("INVALID", "instance UUID")
        ReceiptLedger.initialize(args.root, instance_uuid=args.instance_uuid)
    else:
        service = from_config(args.config)
        try:
            if args.command == "verify":
                service.ledger.verify_startup()
            else:
                service.ledger.rotate_key(
                    args.new_key_id, _read_private(args.new_private)
                )
        finally:
            service.ledger.close()


if __name__ == "__main__":
    main()
