"""Strict version-one receipt encoding and domain-separated hashes.

Only strings, safe JSON integers, lists and objects are admitted. This is the
RFC 8785 subset needed by the v1 contract; accepting floats would require a
separate, reviewed ECMAScript number serializer.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

MAX_REQUEST = 64 * 1024
ZERO_HASH = bytes(32)
EVENT_DOMAIN = b"photodoc-receipt-event-v1\x00"
REQUEST_DOMAIN = b"photodoc-receipt-request-v1\x00"
BODY_DOMAIN = b"photodoc-submission-body-v1\x00"
CHAIN_DOMAIN = b"photodoc-ledger-v1\x00"
SIGN_DOMAIN = b"photodoc-signed-event-v1\x00"
UUID4 = re.compile(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
UTC = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z\Z")
SUFFIX = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
FILE_KEYS = frozenset({
    "upload_id", "ordinal", "client_filename", "display_filename",
    "stored_name", "relative_path", "media_type", "byte_size", "sha256",
    "completion_state", "completed_at_utc",
})
REQUEST_KEYS = frozenset({
    "version", "event_type", "application_instance_uuid", "epoch_id",
    "submission_id", "submission_body_digest", "order_uuid", "order_id",
    "original_count", "files",
})
ORDER_FIELDS = ("name", "phone", "email", "comment", "format", "paper", "crop")


class ReceiptError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _valid_string(value: Any, *, maximum: int = 4096) -> str:
    if type(value) is not str or "\x00" in value:
        raise ReceiptError("INVALID", "invalid string")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise ReceiptError("INVALID", "invalid Unicode") from exc
    if len(encoded) > maximum:
        raise ReceiptError("INVALID", "string exceeds limit")
    return value


def _encode(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if type(value) is int and -(2**53 - 1) <= value <= 2**53 - 1:
        return str(value)
    if type(value) is str:
        _valid_string(value)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if type(value) is list:
        return "[" + ",".join(_encode(item) for item in value) + "]"
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ReceiptError("INVALID", "non-string object key")
        keys = sorted(value, key=lambda key: _valid_string(key).encode("utf-16-be"))
        return "{" + ",".join(_encode(key) + ":" + _encode(value[key]) for key in keys) + "}"
    raise ReceiptError("INVALID", "unsupported JSON value")


def canonical_bytes(value: Any) -> bytes:
    return _encode(value).encode("utf-8")


def parse_canonical(raw: bytes) -> dict:
    if len(raw) > MAX_REQUEST:
        raise ReceiptError("INVALID", "request too large")

    def pairs(items: list[tuple[str, Any]]) -> dict:
        result = {}
        for key, value in items:
            if key in result:
                raise ReceiptError("INVALID", "duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8", "strict"), object_pairs_hook=pairs,
            parse_float=lambda _: (_ for _ in ()).throw(ReceiptError("INVALID", "float")),
            parse_constant=lambda _: (_ for _ in ()).throw(ReceiptError("INVALID", "constant")),
        )
    except ReceiptError:
        raise
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ReceiptError("INVALID", "invalid JSON") from exc
    if type(value) is not dict or canonical_bytes(value) != raw:
        raise ReceiptError("INVALID", "noncanonical JSON")
    return value


def _hex(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if type(value) is not str or not pattern.fullmatch(value):
        raise ReceiptError("INVALID", "invalid " + label)
    return value


def _timestamp(value: Any) -> str:
    if type(value) is not str or not UTC.fullmatch(value):
        raise ReceiptError("INVALID", "invalid timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise ReceiptError("INVALID", "invalid timestamp") from exc
    return value


def validate_request(request: dict) -> dict:
    if type(request) is not dict or frozenset(request) != REQUEST_KEYS:
        raise ReceiptError("INVALID", "request fields")
    if type(request["version"]) is not int or request["version"] != 1:
        raise ReceiptError("UNSUPPORTED_VERSION", "receipt version")
    if request["event_type"] != "UPLOAD_SET_ACCEPTED":
        raise ReceiptError("INVALID", "event type")
    for key in ("application_instance_uuid", "epoch_id", "submission_id", "order_uuid"):
        _hex(request[key], UUID4, key)
    _hex(request["submission_body_digest"], HEX64, "body digest")
    order_id, count = request["order_id"], request["original_count"]
    if type(order_id) is not int or not 0 < order_id <= 2**53 - 1:
        raise ReceiptError("INVALID", "order ID")
    if type(count) is not int or not 1 <= count <= 10:
        raise ReceiptError("INVALID", "original count")
    files = request["files"]
    if type(files) is not list or len(files) != count:
        raise ReceiptError("INVALID", "file count")
    seen_ids = set()
    for ordinal, item in enumerate(files):
        if type(item) is not dict or frozenset(item) != FILE_KEYS:
            raise ReceiptError("INVALID", "file fields")
        upload_id = _hex(item["upload_id"], UUID4, "upload ID")
        if upload_id in seen_ids:
            raise ReceiptError("INVALID", "duplicate upload ID")
        seen_ids.add(upload_id)
        if type(item["ordinal"]) is not int or item["ordinal"] != ordinal:
            raise ReceiptError("INVALID", "ordinal")
        for key in ("client_filename", "display_filename"):
            _valid_string(item[key], maximum=255)
        suffix = SUFFIX.get(item["media_type"]) if type(item["media_type"]) is str else None
        if suffix is None:
            raise ReceiptError("INVALID", "media type")
        stored = f"{order_id}_{upload_id}.{suffix}"
        if item["stored_name"] != stored or item["relative_path"] != "uploads/" + stored:
            raise ReceiptError("INVALID", "stored path")
        if type(item["byte_size"]) is not int or not 1 <= item["byte_size"] <= 20 * 1024 * 1024:
            raise ReceiptError("INVALID", "file size")
        _hex(item["sha256"], HEX64, "file hash")
        if item["completion_state"] != "PUBLISHED":
            raise ReceiptError("INVALID", "completion state")
        _timestamp(item["completed_at_utc"])
    if len(canonical_bytes(request)) > MAX_REQUEST:
        raise ReceiptError("INVALID", "request too large")
    return request


def body_digest(order_fields: dict, files: list[dict]) -> str:
    if frozenset(order_fields) != frozenset(ORDER_FIELDS):
        raise ReceiptError("INVALID", "order fields")
    fields = {key: _valid_string(order_fields[key]) for key in ORDER_FIELDS}
    projection = {
        "version": 1, "order": fields, "original_count": len(files),
        "files": [{
            "client_filename": file["client_filename"],
            "media_type": file["media_type"],
            "byte_size": file["byte_size"],
            "sha256": file["sha256"],
        } for file in files],
    }
    return hashlib.sha256(BODY_DOMAIN + canonical_bytes(projection)).hexdigest()


def request_digest(request: dict) -> bytes:
    return hashlib.sha256(REQUEST_DOMAIN + canonical_bytes(validate_request(request))).digest()


def payload_digest(payload: dict) -> bytes:
    return hashlib.sha256(
        EVENT_DOMAIN + payload["event_type"].encode("utf-8") + b"\x00" + canonical_bytes(payload)
    ).digest()


def chain_hash(previous: bytes, sequence: int, digest: bytes, event_type: str, key_id: str) -> bytes:
    return hashlib.sha256(
        CHAIN_DOMAIN + previous + sequence.to_bytes(8, "big") + digest
        + event_type.encode("utf-8") + b"\x00" + key_id.encode("utf-8")
    ).digest()


def signed_bytes(previous: bytes, sequence: int, chain: bytes, digest: bytes,
                 event_type: str, key_id: str) -> bytes:
    return (
        SIGN_DOMAIN + sequence.to_bytes(8, "big") + previous + chain + digest
        + event_type.encode("utf-8") + b"\x00" + key_id.encode("utf-8")
    )
