#!/usr/bin/env python3
"""PhotoDoc AI — canonical SQLite evidence.

The gate's contract is that a verified backup represents *exactly* the current
persistent state of the live database. Anything weaker (row counts, a hand-picked
field list, a schema compared by name) has already produced false PASSes, so this
module compares SQLite state canonically:

* **schema** — every ``sqlite_schema`` object with its stored SQL, plus
  ``table_list``, ``table_xinfo``, ``index_list``/``index_xinfo``,
  ``foreign_key_list``, ``user_version`` and ``application_id``;
* **rows** — every persistent table, keyed by an unambiguous identity, compared in
  both directions with type-tagged values.

Identity rules (spec §2.2). A row key must be unique or the comparison is refused:

* ``WITHOUT ROWID`` → the complete ordered composite primary-key tuple;
* a rowid table with an ``INTEGER PRIMARY KEY`` → that declared column;
* any other rowid table → ``rowid``/``_rowid_``/``oid``, whichever is not shadowed
  by a user column. If all three are shadowed, this fails closed rather than
  falling back to a non-unique first column.

Values are encoded with their type tag, so ``1`` and ``'1'`` differ, and REALs are
compared by their IEEE-754 payload, so ``-0.0`` and ``+0.0`` do not silently
collapse.

Nothing here writes to a database. Every connection is opened with ``mode=ro``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import quote

#: Bumped when the canonical schema or row encoding changes in a way that would
#: make two digests incomparable.
EVIDENCE_FORMAT_VERSION = 1
ROW_ENCODING_VERSION = 1

#: SQLite's own tables. None are skipped: ``sqlite_sequence`` carries the
#: AUTOINCREMENT high-water mark and ``sqlite_stat*`` carries planner statistics
#: that a restore would otherwise silently change.
SQLITE_INTERNAL_PREFIX = "sqlite_"

#: ``PRAGMA table_list`` arrived in 3.37. Below that, an *unknown* pragma returns an
#: empty result instead of raising, which previously wiped every row from the
#: evidence while still reporting PASS.
MIN_SQLITE_VERSION = (3, 37, 0)

#: Object types that participate in the row comparison.
ROW_TABLES = ("table",)
#: Every object type that participates in the schema description.
SCHEMA_OBJECT_TYPES = ("table", "index", "trigger", "view")

#: Rowid aliases SQLite accepts, in the order we prefer them.
ROWID_ALIASES = ("rowid", "_rowid_", "oid")


class EvidenceError(RuntimeError):
    """Raised when canonical evidence cannot be produced."""


class UnsupportedTable(EvidenceError):
    """A table whose identity or contents cannot be compared unambiguously."""


# --------------------------------------------------------------------------- inputs
def readonly_uri(path: Path) -> str:
    """``mode=ro`` URI with the path percent-encoded.

    A raw path lets ``#`` start a fragment (opening a different database) and lets
    ``mode=ro`` fall inside it. This is the encoding independently verified in the
    PR #29 review, reused here rather than reimplemented.
    """
    resolved = Path(path).resolve().as_posix()
    return f"file:{quote(resolved, safe='/:')}?mode=ro"


def open_readonly(path: Path, *, timeout: float = 10.0) -> sqlite3.Connection:
    conn = sqlite3.connect(readonly_uri(path), uri=True, timeout=timeout)
    conn.execute("PRAGMA query_only=ON")
    return conn


def quote_ident(name: str) -> str:
    """Quote an SQL identifier by doubling embedded double quotes."""
    if "\x00" in name:
        raise EvidenceError(f"identifier contains a NUL byte: {name!r}")
    return '"' + name.replace('"', '""') + '"'


# ------------------------------------------------------------------------ encoding
def encode_value(value: Any) -> bytes:
    """Type-tagged, deterministic encoding of one SQLite value."""
    if value is None:
        return b"\x00"
    if isinstance(value, bool):  # sqlite3 returns int; keep the distinction explicit
        return b"\x01" + (b"1" if value else b"0")
    if isinstance(value, int):
        return b"\x01" + str(value).encode("ascii")
    if isinstance(value, float):
        # Bit payload, so -0.0 != +0.0 and NaN payloads compare exactly.
        return b"\x02" + struct.pack(">d", value)
    if isinstance(value, bytes):
        return b"\x03" + value
    if isinstance(value, str):
        return b"\x04" + value.encode("utf-8")
    raise EvidenceError(f"unsupported SQLite value type: {type(value).__name__}")


def encode_row(values: Sequence[Any]) -> bytes:
    parts = [encode_value(v) for v in values]
    out = bytearray()
    for part in parts:
        out += len(part).to_bytes(8, "big")
        out += part
    return bytes(out)


def digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


# --------------------------------------------------------------------------- tables
@dataclass
class TableInfo:
    name: str
    type: str                 # table | view | virtual | shadow
    without_rowid: bool
    strict: bool
    columns: List[Dict[str, Any]] = field(default_factory=list)


def table_inventory(conn: sqlite3.Connection) -> Dict[str, TableInfo]:
    """``PRAGMA table_list`` for the main schema, classified."""
    inventory: Dict[str, TableInfo] = {}
    try:
        rows = conn.execute("PRAGMA table_list").fetchall()
    except sqlite3.OperationalError:
        # Older SQLite: fall back to sqlite_master, without WITHOUT ROWID flags.
        rows = []
        for name, kind, sql in conn.execute(
                "SELECT name, type, sql FROM sqlite_master WHERE type IN ('table','view')"):
            wr = bool(sql and "WITHOUT ROWID" in sql.upper())
            inventory[name] = TableInfo(name=name, type=kind, without_rowid=wr,
                                        strict=bool(sql and "STRICT" in sql.upper()))
        return inventory
    for schema, name, kind, _ncol, wr, strict in rows:
        if schema != "main":
            continue
        inventory[name] = TableInfo(name=name, type=kind, without_rowid=bool(wr),
                                    strict=bool(strict))
    return inventory


def schema_table_names(conn: sqlite3.Connection) -> set[str]:
    """Every ``table`` object recorded in ``sqlite_schema``."""
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}


def sqlite_version_supported() -> Tuple[bool, str]:
    version = sqlite3.sqlite_version_info
    ok = tuple(version) >= MIN_SQLITE_VERSION
    return ok, ".".join(str(part) for part in version)


def table_columns(conn: sqlite3.Connection, table: str) -> List[Dict[str, Any]]:
    """``PRAGMA table_xinfo`` — includes hidden and generated columns."""
    rows = conn.execute(f"PRAGMA table_xinfo({quote_ident(table)})").fetchall()
    columns = []
    for cid, name, ctype, notnull, dflt, pk, hidden in rows:
        columns.append({"cid": cid, "name": name, "type": ctype, "notnull": notnull,
                        "dflt_value": dflt, "pk": pk, "hidden": hidden})
    return columns


# ------------------------------------------------------------------------- identity
@dataclass(frozen=True)
class RowIdentity:
    """How rows of one table are keyed, unambiguously."""

    kind: str                       # pk | integer_pk | rowid
    columns: Tuple[str, ...]        # the ordered key columns
    select_expression: str          # SQL expression(s) for the key
    description: str


def _user_column_names(columns: Sequence[Dict[str, Any]]) -> set[str]:
    return {c["name"].casefold() for c in columns if c["hidden"] in (0, 2, 3)}


def declared_integer_pk_is_rowid_alias(conn: sqlite3.Connection, table: str,
                                       columns: Sequence[Dict[str, Any]]) -> bool:
    """Does a single declared ``INTEGER PRIMARY KEY`` alias the hidden rowid?

    Not always: the historical ``INTEGER PRIMARY KEY DESC`` declaration is *not* an
    alias — the column may be NULL and carries its own uniqueness index, so
    ``rowid`` and the column differ. Reading that from SQL text is fragile, so the
    proof is semantic: a real alias creates **no** primary-key index, while the
    DESC form creates an automatic ``origin='pk'`` index.
    """
    pk = [c for c in columns if c["pk"]]
    if len(pk) != 1 or (pk[0]["type"] or "").strip().upper() != "INTEGER":
        return False
    for row in conn.execute(f"PRAGMA index_list({quote_ident(table)})").fetchall():
        origin = row[3]
        if origin == "pk":
            return False
    return True


def row_identity(table: str, info: TableInfo,
                 columns: Sequence[Dict[str, Any]]) -> RowIdentity:
    """Derive the unique row key, or refuse.

    A composite key collapsed to its first column silently merges distinct rows —
    that was a real false PASS. So a ``WITHOUT ROWID`` table requires the complete
    ordered PK, and a shadowed rowid refuses rather than guessing.

    Every rowid table is keyed by the **hidden rowid**, even when a declared
    ``INTEGER PRIMARY KEY`` exists. That declaration is usually an alias, but not
    for ``INTEGER PRIMARY KEY DESC``, where the column may be NULL while the rowid
    stays distinct — two databases then produced identical evidence for different
    rows. Using the hidden rowid needs no alias proof and is exact for both forms;
    the declared columns are still compared as part of the row.
    """
    pk_columns = [c for c in columns if c["pk"]]
    visible = [c for c in columns if c["hidden"] in (0, 2, 3)]

    if info.without_rowid:
        if not pk_columns:
            raise UnsupportedTable(
                f"table {table!r} is WITHOUT ROWID but declares no primary key")
        ordered = sorted(pk_columns, key=lambda c: c["pk"])
        positions = [c["pk"] for c in ordered]
        if positions != list(range(1, len(ordered) + 1)):
            raise UnsupportedTable(
                f"table {table!r} has a non-contiguous primary-key order: {positions}")
        names = tuple(c["name"] for c in ordered)
        expr = ", ".join(quote_ident(n) for n in names)
        return RowIdentity("pk", names, expr,
                           f"WITHOUT ROWID composite primary key {names}")

    shadowed = {c["name"].casefold() for c in visible}
    for alias in ROWID_ALIASES:
        if alias.casefold() not in shadowed:
            return RowIdentity("rowid", (alias,), quote_ident(alias),
                               f"hidden {alias} rowid")
    raise UnsupportedTable(
        f"table {table!r} shadows every rowid alias ({', '.join(ROWID_ALIASES)}) and has "
        "no single INTEGER PRIMARY KEY: rows cannot be keyed unambiguously")


# ------------------------------------------------------------------------ row access
def _row_select(table: str, key_expr: str,
                columns: Sequence[Dict[str, Any]]) -> str:
    names = ", ".join(quote_ident(c["name"]) for c in columns)
    order = key_expr
    return (f"SELECT {key_expr} AS __key__, {names} FROM {quote_ident(table)} "
            f"ORDER BY {order}")


def iter_rows(conn: sqlite3.Connection, table: str, identity: RowIdentity,
              columns: Sequence[Dict[str, Any]]) -> Iterator[Tuple[Any, bytes, bytes]]:
    """Stream ``(key_tuple, encoded_key, encoded_row)`` in a stable order."""
    visible = [c for c in columns if c["hidden"] in (0, 2, 3)]
    sql = _row_select(table, identity.select_expression, visible)
    cursor = conn.execute(sql)
    key_count = len(identity.columns)
    for row in cursor:
        flat = row[:key_count]
        values = row[key_count:]
        yield (tuple(flat), encode_row(flat), encode_row(values))


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()[0]


def count_distinct_keys(conn: sqlite3.Connection, table: str,
                        identity: RowIdentity) -> int:
    return conn.execute(
        f"SELECT COUNT(*) FROM (SELECT DISTINCT {identity.select_expression} "
        f"FROM {quote_ident(table)})").fetchone()[0]


# --------------------------------------------------------------------------- schema
def canonical_schema(conn: sqlite3.Connection) -> Dict[str, Any]:
    """A complete, versioned description of the persistent schema."""
    inventory = table_inventory(conn)
    objects = []
    for obj_type, name, tbl_name, sql in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table','index','trigger','view') ORDER BY type, name"):
        objects.append({"type": obj_type, "name": name, "tbl_name": tbl_name,
                        "sql": sql})

    tables = {}
    for name, info in sorted(inventory.items()):
        if info.type == "view":
            continue
        table_cols = table_columns(conn, name)
        entry: Dict[str, Any] = {
            "type": info.type,
            "without_rowid": info.without_rowid,
            "strict": info.strict,
            # Recorded because the DESC declaration is not an alias; the row key
            # uses the hidden rowid either way, but the distinction is evidence.
            "integer_pk_aliases_rowid": declared_integer_pk_is_rowid_alias(
                conn, name, table_cols),
            "columns": table_cols,
            "indexes": [],
            "foreign_keys": [],
        }
        # index_list returns (seq, name, unique, origin, partial) on modern SQLite.
        index_rows = conn.execute(f"PRAGMA index_list({quote_ident(name)})").fetchall()
        for index_row in index_rows:
            _seq, index_name, unique, origin, partial = index_row[:5]
            entry["indexes"].append({
                "name": index_name, "unique": unique, "origin": origin,
                "partial": partial,
                "columns": conn.execute(
                    f"PRAGMA index_xinfo({quote_ident(index_name)})").fetchall(),
            })
        for fk in conn.execute(f"PRAGMA foreign_key_list({quote_ident(name)})"):
            entry["foreign_keys"].append(list(fk))
        tables[name] = entry

    payload = {
        "format": EVIDENCE_FORMAT_VERSION,
        "application_id": conn.execute("PRAGMA application_id").fetchone()[0],
        "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
        "objects": objects,
        "tables": tables,
    }
    payload["digest"] = digest_bytes(canonical_json(payload))
    return payload


def schema_digest(schema: Dict[str, Any]) -> str:
    return schema["digest"]


def schema_differences(live: Dict[str, Any], backup: Dict[str, Any]) -> List[str]:
    """Human-readable reasons the two schemas are not identical."""
    reasons: List[str] = []
    if live["application_id"] != backup["application_id"]:
        reasons.append(f"application_id differs: live={live['application_id']} "
                       f"backup={backup['application_id']}")
    if live["user_version"] != backup["user_version"]:
        reasons.append(f"user_version differs: live={live['user_version']} "
                       f"backup={backup['user_version']}")
    if live["digest"] == backup["digest"]:
        return reasons

    live_objects = {(o["type"], o["name"]): o for o in live["objects"]}
    backup_objects = {(o["type"], o["name"]): o for o in backup["objects"]}
    for key in sorted(set(live_objects) - set(backup_objects)):
        reasons.append(f"schema object missing from the backup: {key[0]} {key[1]!r}")
    for key in sorted(set(backup_objects) - set(live_objects)):
        reasons.append(f"schema object present only in the backup: {key[0]} {key[1]!r}")
    for key in sorted(set(live_objects) & set(backup_objects)):
        if live_objects[key] != backup_objects[key]:
            reasons.append(f"schema object differs: {key[0]} {key[1]!r}")

    for table in sorted(set(live["tables"]) | set(backup["tables"])):
        live_t = live["tables"].get(table)
        backup_t = backup["tables"].get(table)
        if live_t is None:
            reasons.append(f"table present only in the backup: {table!r}")
            continue
        if backup_t is None:
            reasons.append(f"table missing from the backup: {table!r}")
            continue
        for field_name in ("type", "without_rowid", "strict", "columns",
                           "indexes", "foreign_keys"):
            if live_t.get(field_name) != backup_t.get(field_name):
                reasons.append(
                    f"table {table!r} differs in {field_name}: restoring the backup would "
                    "change behaviour even if the rows match")
    if not reasons:
        reasons.append("schema digests differ but no individual difference was isolated")
    return reasons


# --------------------------------------------------------------------------- digest
def logical_state_digest(conn: sqlite3.Connection) -> str:
    """Content digest of one database, in the same shape the comparison reports.

    Exposed so that backup creation and the gate compute the digest with one
    implementation: a second copy of this logic silently drifted once already.
    """
    inventory = table_inventory(conn)
    parts: List[bytes] = []
    for table in sorted(inventory):
        info = inventory[table]
        if info.type in ("view", "virtual"):
            continue
        columns = table_columns(conn, table)
        identity = row_identity(table, info, columns)
        content = hashlib.sha256()
        for _key, key_encoded, row_encoded in iter_rows(conn, table, identity, columns):
            content.update(key_encoded + row_encoded)
        parts.append(canonical_json({
            "table": table, "identity": identity.description,
            "rows": count_rows(conn, table), "content": content.hexdigest()}))
    return digest_bytes(b"".join(parts))


# --------------------------------------------------------------------------- compare
@dataclass
class TableComparison:
    table: str
    identity: str
    live_rows: int = 0
    backup_rows: int = 0
    compared: int = 0
    missing: List[Any] = field(default_factory=list)
    extra: List[Any] = field(default_factory=list)
    changed: List[Any] = field(default_factory=list)
    #: Content digests, so the logical digest reflects row values and not only counts.
    live_digest: str = ""
    backup_digest: str = ""

    @property
    def ok(self) -> bool:
        return not (self.missing or self.extra or self.changed)


@dataclass
class EvidenceResult:
    ok: bool
    live_path: str
    backup_path: str
    schema_digest: Optional[str] = None
    backup_schema_digest: Optional[str] = None
    logical_digest: Optional[str] = None
    backup_logical_digest: Optional[str] = None
    tables: Dict[str, TableComparison] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    checks: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "format": EVIDENCE_FORMAT_VERSION,
            "live_path": self.live_path,
            "backup_path": self.backup_path,
            "schema_digest": self.schema_digest,
            "backup_schema_digest": self.backup_schema_digest,
            "logical_digest": self.logical_digest,
            "backup_logical_digest": self.backup_logical_digest,
            "reasons": self.reasons,
            "checks": self.checks,
            "tables": {
                name: {
                    "identity": cmp.identity,
                    "live_rows": cmp.live_rows,
                    "backup_rows": cmp.backup_rows,
                    "compared": cmp.compared,
                    "missing": cmp.missing[:5],
                    "extra": cmp.extra[:5],
                    "changed": cmp.changed[:5],
                    "live_content_digest": cmp.live_digest,
                    "backup_content_digest": cmp.backup_digest,
                }
                for name, cmp in self.tables.items()
            },
        }


def _compare_table(live: sqlite3.Connection, backup: sqlite3.Connection,
                   table: str, identity: RowIdentity, columns) -> TableComparison:
    result = TableComparison(table=table, identity=identity.description)
    result.live_rows = count_rows(live, table)
    result.backup_rows = count_rows(backup, table)

    for label, conn, expected in (("live", live, result.live_rows),
                                  ("backup", backup, result.backup_rows)):
        distinct = count_distinct_keys(conn, table, identity)
        if distinct != expected:
            raise UnsupportedTable(
                f"table {table!r} has {expected} rows but only {distinct} distinct keys on "
                f"the {label} side: the chosen identity is not unique")

    live_hash = hashlib.sha256()
    backup_hash = hashlib.sha256()
    live_iter = iter_rows(live, table, identity, columns)
    backup_iter = iter_rows(backup, table, identity, columns)
    left = next(live_iter, None)
    right = next(backup_iter, None)
    while left is not None or right is not None:
        if left is None:
            backup_hash.update(right[1] + right[2])
            result.extra.append(right[1].hex()[:32])
            right = next(backup_iter, None)
            continue
        if right is None:
            live_hash.update(left[1] + left[2])
            result.missing.append(left[1].hex()[:32])
            left = next(live_iter, None)
            continue
        if left[1] == right[1]:            # identical key
            live_hash.update(left[1] + left[2])
            backup_hash.update(right[1] + right[2])
            result.compared += 1
            if left[2] != right[2]:
                result.changed.append({"key": left[1].hex()[:32],
                                       "detail": "row values differ"})
            left = next(live_iter, None)
            right = next(backup_iter, None)
            continue
        # Keys differ: decide which side is ahead using SQLite's own ordering.
        order = _key_order(live, table, identity, left[0], right[0])
        if order <= 0:
            live_hash.update(left[1] + left[2])
            result.missing.append(left[1].hex()[:32])
            left = next(live_iter, None)
        else:
            backup_hash.update(right[1] + right[2])
            result.extra.append(right[1].hex()[:32])
            right = next(backup_iter, None)
    result.live_digest = live_hash.hexdigest()
    result.backup_digest = backup_hash.hexdigest()
    return result


def _key_order(conn: sqlite3.Connection, table: str, identity: RowIdentity,
               left: Tuple[Any, ...], right: Tuple[Any, ...]) -> int:
    """Compare two keys using SQLite's ordering, not Python's.

    Both row streams come from the same ``ORDER BY`` expression on their own
    database, so a row-value comparison is enough to decide which stream is
    behind. Python's ordering would disagree with SQLite across types.
    """
    placeholders = ", ".join("?" for _ in identity.columns)
    row = conn.execute(
        f"SELECT CASE WHEN ({placeholders}) < ({placeholders}) THEN -1 "
        f"WHEN ({placeholders}) > ({placeholders}) THEN 1 ELSE 0 END",
        (*left, *right, *left, *right)).fetchone()
    return int(row[0]) if row else 0


def compare_databases(live_path: Path, backup_path: Path, *,
                      expected_application_id: int,
                      policy: str = "normal") -> EvidenceResult:
    """Compare the live database with a backup database, canonically and two-way.

    ``policy`` is ``normal`` or ``pre-outbox-transition``. The transition policy
    only affects which *table* differences are allowed: the legacy backup and the
    frozen live database must still have identical schemas and identical rows.
    """
    result = EvidenceResult(ok=False, live_path=str(live_path), backup_path=str(backup_path))
    if policy not in ("normal", "pre-outbox-transition"):
        result.reasons.append(f"unknown policy {policy!r}")
        return result
    for label, path in (("live", live_path), ("backup", backup_path)):
        if not Path(path).is_file():
            result.reasons.append(f"{label} database not found: {path}")
            return result

    supported, version = sqlite_version_supported()
    result.checks["sqlite_version"] = version
    if not supported:
        result.reasons.append(
            f"SQLite {version} is older than the required "
            f"{'.'.join(str(p) for p in MIN_SQLITE_VERSION)}: PRAGMA table_list is "
            "unavailable, and an unsupported pragma returns an empty result rather "
            "than raising, which would erase every row from the evidence")
        return result

    try:
        live = open_readonly(Path(live_path))
        backup = open_readonly(Path(backup_path))
    except sqlite3.Error as exc:
        result.reasons.append(f"cannot open a database read-only: {exc}")
        return result

    try:
        # H2: one explicit read transaction per side, held for the whole capture.
        # Without BEGIN every PRAGMA/SELECT is its own transaction, so the gate can
        # combine rows that never coexisted (two tables compared either side of an
        # atomic writer commit). Each side is then internally consistent for the
        # whole capture; the two sides are still not simultaneously frozen, which
        # is exactly why Stage 2 must exclude writers.
        for label, conn in (("live", live), ("backup", backup)):
            try:
                conn.execute("BEGIN")
            except sqlite3.Error as exc:
                result.reasons.append(
                    f"cannot open a read transaction on the {label} database: {exc}")
                return result

        for label, conn in (("live", live), ("backup", backup)):
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            result.checks[f"{label}_integrity"] = integrity
            if integrity != "ok":
                result.reasons.append(f"{label} integrity_check failed: {integrity}")
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
            result.checks[f"{label}_foreign_key_violations"] = len(fk)
            if fk:
                result.reasons.append(f"{label} has {len(fk)} foreign-key violation(s)")
            app_id = conn.execute("PRAGMA application_id").fetchone()[0]
            result.checks[f"{label}_application_id"] = app_id
            if app_id != expected_application_id:
                result.reasons.append(
                    f"{label} application_id is {app_id}, expected {expected_application_id}")
        if result.reasons:
            return result

        live_schema = canonical_schema(live)
        backup_schema = canonical_schema(backup)
        result.schema_digest = live_schema["digest"]
        result.backup_schema_digest = backup_schema["digest"]
        schema_reasons = schema_differences(live_schema, backup_schema)
        if schema_reasons:
            result.reasons.extend(schema_reasons)
            return result

        live_inventory = table_inventory(live)
        backup_inventory = table_inventory(backup)
        for label, conn, inventory in (("live", live, live_inventory),
                                       ("backup", backup, backup_inventory)):
            declared = schema_table_names(conn)
            absent = sorted(declared - set(inventory))
            if absent:
                result.reasons.append(
                    f"table introspection on the {label} database omitted "
                    f"{len(absent)} table(s) present in sqlite_schema "
                    f"({', '.join(absent[:3])}): the engine's metadata is incomplete, "
                    "so no row evidence can be trusted")
            if not inventory and declared:
                result.reasons.append(
                    f"PRAGMA table_list returned nothing for the {label} database "
                    f"although sqlite_schema declares {len(declared)} table(s)")
        if result.reasons:
            return result
        if set(live_inventory) != set(backup_inventory):
            result.reasons.append("the two databases expose different tables")
            return result

        live_logical: List[bytes] = []
        backup_logical: List[bytes] = []
        for table in sorted(live_inventory):
            info = live_inventory[table]
            if info.type in ("view",):
                continue
            if info.type == "virtual":
                # A virtual table's state may live outside SQLite. Its shadow tables
                # are compared as ordinary tables; the module itself is schema.
                result.reasons.append(
                    f"table {table!r} is a virtual table: its contents cannot be compared "
                    "canonically; an explicit module adapter is required")
                continue
            if policy == "pre-outbox-transition" and table == "paid_email_outbox":
                result.reasons.append(
                    "the pre-outbox transition requires both databases to lack "
                    "paid_email_outbox")
                continue
            columns = table_columns(live, table)
            try:
                identity = row_identity(table, info, columns)
                comparison = _compare_table(live, backup, table, identity, columns)
            except UnsupportedTable as exc:
                # Fail closed: an unkeyable table is a refusal, not a skip. Reporting
                # it as a reason keeps the gate's answer a blocking one.
                result.reasons.append(str(exc))
                continue
            result.tables[table] = comparison
            if comparison.missing:
                result.reasons.append(
                    f"table {table!r}: {len(comparison.missing)} row(s) present only in the "
                    "live database; restoring the backup would delete them")
            if comparison.extra:
                result.reasons.append(
                    f"table {table!r}: {len(comparison.extra)} row(s) present only in the "
                    "backup; restoring it would resurrect deleted state")
            if comparison.changed:
                result.reasons.append(
                    f"table {table!r}: {len(comparison.changed)} row(s) differ in value")
            live_logical.append(canonical_json({
                "table": table, "identity": comparison.identity,
                "rows": comparison.live_rows, "content": comparison.live_digest}))
            backup_logical.append(canonical_json({
                "table": table, "identity": comparison.identity,
                "rows": comparison.backup_rows, "content": comparison.backup_digest}))

        result.logical_digest = digest_bytes(b"".join(live_logical))
        result.backup_logical_digest = digest_bytes(b"".join(backup_logical))
        result.ok = not result.reasons
        return result
    except (sqlite3.DatabaseError, sqlite3.Error, OSError, UnicodeError) as exc:
        # M5: a corrupt or unreadable database is a *structured* refusal. Escaping
        # as an exception gave a nonzero exit with no reason, and a caller that
        # only checks `ok` would see the default False but no explanation.
        result.ok = False
        result.reasons.append(
            f"the databases could not be read as SQLite evidence: "
            f"{type(exc).__name__}: {exc}")
        return result
    finally:
        for conn in (live, backup):
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        live.close()
        backup.close()
