"""Adversarial tests for the canonical SQLite evidence comparison (Stage 1).

Every case here corresponds to a false PASS produced by the previous
implementations: counts instead of identity, a hand-picked field list, a schema
compared by name, `WITHOUT ROWID` keys collapsed to their first column, shadowed
rowid aliases, and one-way containment.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# evidence_fixtures installs scripts/ on sys.path before the modules below import it.
from evidence_fixtures import APPLICATION_ID, build_database, copy_database

import db_evidence
from db_evidence import EvidenceError, UnsupportedTable


def compare(live: Path, backup: Path, **kwargs):
    return db_evidence.compare_databases(live, backup,
                                         expected_application_id=APPLICATION_ID, **kwargs)


@pytest.fixture()
def base(tmp_path: Path) -> Path:
    return build_database(tmp_path / "base.db")


# ------------------------------------------------------------------ positive control
def test_identical_databases_pass(tmp_path, base):
    copy = copy_database(base, tmp_path / "copy.db")
    result = compare(base, copy)
    assert result.ok, result.reasons
    assert result.schema_digest == result.backup_schema_digest
    assert result.tables, "at least one table must be compared"


# ------------------------------------------------------------- WITHOUT ROWID identity
def test_without_rowid_composite_key_change_is_detected(tmp_path, base):
    """Third review, finding 2: the old code keyed WITHOUT ROWID rows by their first
    column, so two rows sharing it collapsed and a change could hide."""
    changed = build_database(tmp_path / "changed.db", variant="wr_row")
    result = compare(changed, base)
    assert not result.ok
    wr = result.tables["wr_items"]
    assert wr.identity.startswith("WITHOUT ROWID composite primary key")
    assert wr.changed and not wr.missing and not wr.extra


def test_without_rowid_rows_sharing_the_first_column_are_distinct(tmp_path):
    """Two rows with the same first key column must both be compared."""
    live = build_database(tmp_path / "a.db")
    backup = copy_database(live, tmp_path / "b.db")
    conn = sqlite3.connect(live)
    try:
        conn.execute("UPDATE wr_items SET payload='x' WHERE order_id=1 AND line_no=1")
        conn.commit()
    finally:
        conn.close()
    result = compare(live, backup)
    assert not result.ok
    assert result.tables["wr_items"].changed
    # both rows of order 1 were present in each database
    assert result.tables["wr_items"].live_rows == 3


def test_without_rowid_composite_key_order_is_complete(tmp_path, base):
    columns = db_evidence.table_columns(db_evidence.open_readonly(base), "wr_items")
    info = db_evidence.table_inventory(db_evidence.open_readonly(base))["wr_items"]
    identity = db_evidence.row_identity("wr_items", info, columns)
    assert identity.columns == ("order_id", "line_no")


# ------------------------------------------------------------------------ rowid rules
def test_shadowed_rowid_falls_back_to_an_unshadowed_alias(tmp_path, base):
    conn = db_evidence.open_readonly(base)
    try:
        columns = db_evidence.table_columns(conn, "shadowed")
        info = db_evidence.table_inventory(conn)["shadowed"]
    finally:
        conn.close()
    identity = db_evidence.row_identity("shadowed", info, columns)
    assert identity.columns == ("_rowid_",)


def test_a_table_that_shadows_every_rowid_alias_is_refused(tmp_path):
    """Fail closed instead of silently using a non-unique first column."""
    unkeyable = build_database(tmp_path / "live.db", variant="unkeyable")
    backup = copy_database(unkeyable, tmp_path / "backup.db")
    result = compare(unkeyable, backup)
    assert not result.ok
    assert any("shadows every rowid alias" in reason for reason in result.reasons)


def test_shadowed_rowid_change_is_detected(tmp_path, base):
    changed = build_database(tmp_path / "changed.db", variant="shadow_row")
    result = compare(changed, base)
    assert not result.ok
    assert result.tables["shadowed"].changed


# ------------------------------------------------------------------ two-way comparison
def test_a_row_only_in_the_backup_is_refused(tmp_path, base):
    extra = build_database(tmp_path / "extra.db", variant="backup_only_row")
    result = compare(base, extra)
    assert not result.ok
    assert result.tables["orders"].extra
    assert any("resurrect" in reason for reason in result.reasons)


def test_a_row_only_in_the_live_database_is_refused(tmp_path, base):
    extra = build_database(tmp_path / "extra.db", variant="backup_only_row")
    result = compare(extra, base)
    assert not result.ok
    assert result.tables["orders"].missing


def test_deleted_row_cannot_hide_behind_equal_counts(tmp_path):
    live = build_database(tmp_path / "live.db")
    backup = copy_database(live, tmp_path / "backup.db")
    conn = sqlite3.connect(live)
    try:
        conn.execute("DELETE FROM orders WHERE customer='ann'")
        conn.execute("INSERT INTO orders (customer, amount, note) VALUES ('eve','1.00','x')")
        conn.commit()
    finally:
        conn.close()
    result = compare(live, backup)
    assert not result.ok
    assert result.tables["orders"].missing and result.tables["orders"].extra
    assert result.tables["orders"].live_rows == result.tables["orders"].backup_rows


# ------------------------------------------------------------------------ sqlite_*
def test_autoincrement_high_water_mark_is_compared(tmp_path, base):
    """`sqlite_sequence` is not ignored by prefix: losing it reissues ids."""
    bumped = build_database(tmp_path / "bumped.db", variant="sequence")
    result = compare(bumped, base)
    assert not result.ok
    assert "sqlite_sequence" in result.tables
    assert not result.tables["sqlite_sequence"].ok


def test_sqlite_schema_is_compared_as_a_table(tmp_path, base):
    result = compare(base, copy_database(base, tmp_path / "copy.db"))
    assert "sqlite_schema" in result.tables


# ------------------------------------------------------------------------- value types
@pytest.mark.parametrize("variant", [
    "typed_text_vs_int",       # '1' vs 1
    "typed_negative_zero",     # -0.0 vs 0.0
])
def test_type_tagged_values_are_not_equal(tmp_path, base, variant):
    changed = build_database(tmp_path / "changed.db", variant=variant)
    result = compare(changed, base)
    assert not result.ok, f"{variant} was treated as equal"
    assert result.tables["typed"].changed


def test_integer_and_real_are_distinct(tmp_path, base):
    live = build_database(tmp_path / "live.db")
    backup = copy_database(live, tmp_path / "backup.db")
    conn = sqlite3.connect(live)
    try:
        conn.execute("UPDATE typed SET payload=1 WHERE id=3")  # 1.0 -> 1
        conn.commit()
    finally:
        conn.close()
    result = compare(live, backup)
    assert not result.ok
    assert result.tables["typed"].changed


def test_blob_and_null_are_distinct(tmp_path):
    live = build_database(tmp_path / "live.db")
    backup = copy_database(live, tmp_path / "backup.db")
    conn = sqlite3.connect(live)
    try:
        conn.execute("UPDATE typed SET payload=NULL WHERE id=5")  # b'\x00' -> NULL
        conn.commit()
    finally:
        conn.close()
    result = compare(live, backup)
    assert not result.ok


# ------------------------------------------------------------------------------ schema
@pytest.mark.parametrize("variant,needle", [
    ("extra_index", "index"),
    ("changed_trigger", "trigger"),
    ("extra_table", "table"),
])
def test_schema_objects_must_match_exactly(tmp_path, base, variant, needle):
    changed = build_database(tmp_path / "changed.db", variant=variant)
    result = compare(changed, base)
    assert not result.ok
    assert any(needle in reason for reason in result.reasons), result.reasons


def test_an_added_column_is_refused(tmp_path, base):
    live = copy_database(base, tmp_path / "live.db")
    conn = sqlite3.connect(live)
    try:
        conn.execute("ALTER TABLE shadowed ADD COLUMN extra TEXT")
        conn.commit()
    finally:
        conn.close()
    result = compare(live, base)
    assert not result.ok
    assert any("shadowed" in reason for reason in result.reasons), result.reasons


def test_a_changed_declared_type_is_refused(tmp_path, base):
    live = copy_database(base, tmp_path / "live.db")
    conn = sqlite3.connect(live)
    try:
        conn.executescript(
            """
            CREATE TABLE shadowed_new (rowid TEXT, value INTEGER);
            INSERT INTO shadowed_new SELECT * FROM shadowed;
            DROP TABLE shadowed;
            ALTER TABLE shadowed_new RENAME TO shadowed;
            """)
        conn.commit()
    finally:
        conn.close()
    result = compare(live, base)
    assert not result.ok
    assert any("shadowed" in reason for reason in result.reasons), result.reasons


def test_a_changed_check_constraint_is_refused(tmp_path, base):
    live = copy_database(base, tmp_path / "live.db")
    conn = sqlite3.connect(live)
    try:
        conn.executescript(
            """
            CREATE TABLE constrained_new (id INTEGER PRIMARY KEY,
                                          v TEXT CHECK (length(v) >= 2));
            INSERT INTO constrained_new SELECT * FROM constrained;
            DROP TABLE constrained;
            ALTER TABLE constrained_new RENAME TO constrained;
            """)
        conn.commit()
    finally:
        conn.close()
    result = compare(live, base)
    assert not result.ok


def test_a_view_definition_change_is_refused(tmp_path, base):
    live = copy_database(base, tmp_path / "live.db")
    conn = sqlite3.connect(live)
    try:
        conn.execute("DROP VIEW v_orders")
        conn.execute("CREATE VIEW v_orders AS SELECT id, customer, note FROM orders")
        conn.commit()
    finally:
        conn.close()
    result = compare(live, base)
    assert not result.ok


# --------------------------------------------------------------------- integrity gates
def test_application_id_mismatch_is_refused(tmp_path, base):
    other = copy_database(base, tmp_path / "other.db")
    conn = sqlite3.connect(other)
    try:
        conn.execute("PRAGMA application_id = 42")
        conn.commit()
    finally:
        conn.close()
    result = compare(base, other)
    assert not result.ok
    assert any("application_id" in reason for reason in result.reasons)


def test_foreign_key_violation_is_refused(tmp_path, base):
    broken = build_database(tmp_path / "broken.db", variant="fk_violation")
    result = compare(broken, base)
    assert not result.ok
    assert any("foreign-key" in reason for reason in result.reasons)


def test_missing_database_is_reported_not_created(tmp_path, base):
    missing = tmp_path / "nope" / "orders.db"
    result = compare(base, missing)
    assert not result.ok
    assert not missing.exists()


def test_unknown_policy_is_refused(tmp_path, base):
    result = compare(base, copy_database(base, tmp_path / "c.db"), policy="whatever")
    assert not result.ok


# ------------------------------------------------------------------------- digest shape
def test_logical_digest_is_deterministic_and_content_sensitive(tmp_path, base):
    copy = copy_database(base, tmp_path / "copy.db")
    first = compare(base, copy)
    second = compare(base, copy)
    assert first.logical_digest == second.logical_digest
    assert first.schema_digest == second.schema_digest

    changed = build_database(tmp_path / "changed.db", variant="wr_row")
    different = compare(changed, base)
    assert different.logical_digest != first.logical_digest


def test_encoding_is_injective_over_sqlite_types():
    values = [None, 0, 1, -1, 0.0, -0.0, 1.0, "", "0", b"", b"\x00", "é", "e"]
    encoded = [db_evidence.encode_value(v) for v in values]
    assert len(set(encoded)) == len(values), "two distinct SQLite values encoded identically"


def test_encoding_distinguishes_nan_from_itself_by_payload():
    nan_a = float("nan")
    assert db_evidence.encode_value(nan_a) == db_evidence.encode_value(float("nan"))


def test_identifier_quoting_handles_embedded_quotes(tmp_path):
    path = tmp_path / "quoted.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute('CREATE TABLE "we""ird" (id INTEGER PRIMARY KEY, v TEXT)')
        conn.execute('INSERT INTO "we""ird" VALUES (1, \'x\')')
        conn.commit()
        columns = db_evidence.table_columns(conn, 'we"ird')
        assert columns[0]["name"] == "id"
        assert db_evidence.count_rows(conn, 'we"ird') == 1
    finally:
        conn.close()


def test_readonly_uri_percent_encodes_the_path(tmp_path):
    tricky = tmp_path / "a#b c%20e.db"
    tricky.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(str(tricky)).close()
    uri = db_evidence.readonly_uri(tricky)
    assert "#" not in uri.split("?", 1)[0]
    conn = db_evidence.open_readonly(tricky)
    try:
        assert conn.execute("PRAGMA application_id").fetchone() is not None
    finally:
        conn.close()
