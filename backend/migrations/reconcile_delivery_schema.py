"""P1 additive migration. Apply only to a backed-up PhotoDoc database with the writer stopped."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db_contract import APPLICATION_ID, inspect_database  # noqa: E402
from delivery import DELIVERY_SCHEMA, acquire_migration_gate, release_migration_gate  # noqa: E402

REQUIRED_CLEANUP_COLUMNS = {
    "cleanup_state",
    "cleanup_phase",
    "local_deleted_at",
    "cleanup_error",
    "cleanup_attempts",
    "cleanup_lease_until",
}


def reconcile(db_path: Path, apply: bool = False, uploads_dir: Path | None = None) -> dict:
    ready, details = inspect_database(db_path, require_complete_schema=True)
    if not ready or details["application_id"] != APPLICATION_ID:
        raise RuntimeError("P0 PhotoDoc database identity/schema check failed")

    gate_holder = None
    if apply:
        gate_holder = acquire_migration_gate(db_path)

    if uploads_dir is None:
        env_dir = os.getenv("UPLOAD_DIR")
        if env_dir:
            uploads_dir = Path(env_dir)
        elif (db_path.parent / "uploads").is_dir():
            uploads_dir = db_path.parent / "uploads"
        else:
            uploads_dir = Path("/app/uploads")

    conn = sqlite3.connect(db_path, timeout=10)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_jobs'").fetchone()
        delivery_cols = {row[1] for row in conn.execute("PRAGMA table_info(delivery_jobs)")} if table else set()
        indexes = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='delivery_jobs'"
        )} if table else set()

        unrepaired_count = 0
        if table:
            query_parts = []
            if "cleanup_phase" in delivery_cols and "artifact_role" in delivery_cols:
                query_parts.append("(cleanup_phase = 'none' AND kind IN ('file', 'info') AND state = 'delivered' AND artifact_role = 'result')")
            elif "cleanup_phase" in delivery_cols:
                query_parts.append("(cleanup_phase = 'none' AND kind IN ('file', 'info') AND state = 'delivered')")
            if "cleanup_state" in delivery_cols and "local_deleted_at" in delivery_cols:
                query_parts.append(
                    "(cleanup_state = 'completed' AND kind IN ('file', 'info') AND state = 'delivered' "
                    "AND (local_deleted_at IS NULL OR local_deleted_at = delivered_at))"
                )
            if query_parts:
                unrepaired_query = f"SELECT COUNT(*) FROM delivery_jobs WHERE {' OR '.join(query_parts)}"
                unrepaired_count = conn.execute(unrepaired_query).fetchone()[0]

        unclassified_role_count = 0
        if table is not None and "artifact_role" in delivery_cols:
            has_order_results = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_results'"
            ).fetchone() is not None
            if has_order_results:
                unclassified_role_count = conn.execute(
                    "SELECT COUNT(*) FROM delivery_jobs "
                    "WHERE ((artifact_role IS NULL OR artifact_role IN ('', 'unknown')) AND kind IN ('file', 'info')) "
                    "OR (kind = 'file' AND artifact_role = 'result' AND ("
                    "  remote_path NOT LIKE '%_result.bin' OR remote_path LIKE '%_source%' OR "
                    "  NOT EXISTS (SELECT 1 FROM order_results r WHERE r.order_id = delivery_jobs.order_id "
                    "              AND r.path = delivery_jobs.local_path AND r.sha256 = delivery_jobs.content_sha256)"
                    ")) "
                    "OR (kind = 'file' AND artifact_role = 'source' AND ("
                    "  remote_path LIKE '%_result.bin' OR "
                    "  EXISTS (SELECT 1 FROM order_results r WHERE r.order_id = delivery_jobs.order_id "
                    "          AND (r.path = delivery_jobs.local_path OR r.sha256 = delivery_jobs.content_sha256))"
                    "))"
                ).fetchone()[0]
            else:
                unclassified_role_count = conn.execute(
                    "SELECT COUNT(*) FROM delivery_jobs "
                    "WHERE ((artifact_role IS NULL OR artifact_role IN ('', 'unknown')) AND kind IN ('file', 'info')) "
                    "OR (kind = 'file' AND artifact_role = 'result') "
                    "OR (kind = 'file' AND artifact_role = 'source' AND remote_path LIKE '%_result.bin')"
                ).fetchone()[0]

        needed = (
            "order_token_hash" not in columns
            or table is None
            or not REQUIRED_CLEANUP_COLUMNS <= delivery_cols
            or "artifact_role" not in delivery_cols
            or not {"idx_delivery_due", "idx_cleanup_due", "idx_paid_email_once", "idx_admin_email_once"} <= indexes
            or unrepaired_count > 0
            or unclassified_role_count > 0
        )

        if apply and needed:
            conn.execute("BEGIN IMMEDIATE")
            if "order_token_hash" not in columns:
                conn.execute("ALTER TABLE orders ADD COLUMN order_token_hash TEXT")

            if table is None:
                for statement in DELIVERY_SCHEMA.split(";"):
                    if statement.strip():
                        conn.execute(statement)
            else:
                if "cleanup_state" not in delivery_cols:
                    conn.execute("ALTER TABLE delivery_jobs ADD COLUMN cleanup_state TEXT NOT NULL DEFAULT 'pending'")
                if "cleanup_phase" not in delivery_cols:
                    conn.execute("ALTER TABLE delivery_jobs ADD COLUMN cleanup_phase TEXT NOT NULL DEFAULT 'none'")
                if "local_deleted_at" not in delivery_cols:
                    conn.execute("ALTER TABLE delivery_jobs ADD COLUMN local_deleted_at TEXT")
                if "cleanup_error" not in delivery_cols:
                    conn.execute("ALTER TABLE delivery_jobs ADD COLUMN cleanup_error TEXT")
                if "cleanup_attempts" not in delivery_cols:
                    conn.execute("ALTER TABLE delivery_jobs ADD COLUMN cleanup_attempts INTEGER NOT NULL DEFAULT 0")
                if "cleanup_lease_until" not in delivery_cols:
                    conn.execute("ALTER TABLE delivery_jobs ADD COLUMN cleanup_lease_until TEXT")
                if "artifact_role" not in delivery_cols:
                    conn.execute("ALTER TABLE delivery_jobs ADD COLUMN artifact_role TEXT")

                # Authoritative classification of artifact_role
                has_order_results = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_results'"
                ).fetchone() is not None

                results_by_order = {}
                if has_order_results:
                    for r in conn.execute("SELECT order_id, path, sha256, size FROM order_results").fetchall():
                        results_by_order[r[0]] = {"path": r[1], "sha256": r[2], "size": r[3]}

                conn.execute(
                    "UPDATE delivery_jobs SET artifact_role = 'info' "
                    "WHERE (artifact_role IS NULL OR artifact_role = '') AND kind = 'info'"
                )

                file_jobs = conn.execute(
                    "SELECT id, order_id, local_path, original_name, remote_path, content_sha256, artifact_role, state "
                    "FROM delivery_jobs WHERE kind = 'file'"
                ).fetchall()

                for job in file_jobs:
                    jid = job[0]
                    oid = job[1]
                    lpath = job[2] or ""
                    orig_name = job[3] or ""
                    rpath = job[4] or ""
                    jsha = job[5] or ""
                    cur_role = job[6]
                    jstate = job[7]

                    ores = results_by_order.get(oid)

                    ends_result = rpath.endswith("_result.bin")
                    has_source_marker = "_source" in rpath.lower() or "_source" in lpath.lower()
                    has_result_marker = "_result" in rpath.lower() or "_result" in lpath.lower()

                    path_match = bool(lpath and ores and lpath == ores["path"])
                    sha_match = bool(jsha and ores and jsha == ores["sha256"])
                    job_matches_ores = bool(ores is not None and path_match and sha_match)
                    job_conflicts_ores = bool(ores is not None and (path_match or sha_match))

                    if cur_role == "quarantine":
                        # Explicit quarantine classifications must never be promoted
                        new_role = "quarantine"
                    elif cur_role == "source":
                        # Explicit source classifications must never be promoted to result.
                        # If metadata conflicts with result (e.g. ends with _result.bin or matches/conflicts with ores),
                        # fail closed to quarantine. Otherwise remain source.
                        if ends_result or job_conflicts_ores or has_result_marker:
                            new_role = "quarantine"
                        else:
                            new_role = "source"
                    elif job_matches_ores and ends_result and not has_source_marker:
                        # Authoritative evidence for completed result
                        new_role = "result"
                    elif job_conflicts_ores:
                        # Partial conflict with order_results (e.g. path matches but hash disagrees)
                        new_role = "quarantine"
                    elif ends_result or has_result_marker:
                        # Result-like naming but lacks authoritative order_results evidence
                        new_role = "quarantine"
                    elif jstate == "delivered":
                        # Historical delivered job without authoritative order_results evidence
                        new_role = "quarantine"
                    elif lpath:
                        # Clean, unambiguous source artifact
                        new_role = "source"
                    else:
                        new_role = "quarantine"

                    if cur_role != new_role:
                        conn.execute("UPDATE delivery_jobs SET artifact_role = ? WHERE id = ?", (new_role, jid))

                conn.execute("CREATE INDEX IF NOT EXISTS idx_cleanup_due ON delivery_jobs(state, cleanup_state, cleanup_lease_until)")
                conn.execute("UPDATE delivery_jobs SET cleanup_state='not_applicable', cleanup_phase='none' WHERE kind NOT IN ('file', 'info')")

                # Historical backfill and repair: inspect physical filesystem for delivered RESULT file/info jobs
                # Never assume delivered implies cleaned or invent local_deleted_at
                delivered_rows = conn.execute(
                    "SELECT id, local_path, cleanup_state, cleanup_phase, local_deleted_at, delivered_at, artifact_role "
                    "FROM delivery_jobs WHERE state='delivered' AND kind IN ('file', 'info')"
                ).fetchall()
                for job_id, local_path, cur_cstate, cur_cphase, cur_deleted_at, deliv_at, arole in delivered_rows:
                    file_exists = False
                    if local_path:
                        p = Path(local_path)
                        if p.is_file():
                            file_exists = True
                        elif uploads_dir is not None and (uploads_dir / p.name).is_file():
                            file_exists = True

                    if file_exists:
                        if arole in ("result", "info"):
                            # Verified result/info file physically exists: schedule for verified cleanup
                            conn.execute(
                                "UPDATE delivery_jobs SET cleanup_state='pending', cleanup_phase='pending', "
                                "local_deleted_at=NULL, cleanup_error=NULL WHERE id=?",
                                (job_id,),
                            )
                        else:
                            # Source or quarantine file physically exists: must NEVER be deleted by delivery cleanup
                            conn.execute(
                                "UPDATE delivery_jobs SET cleanup_state='not_applicable', cleanup_phase='none', "
                                "local_deleted_at=NULL, cleanup_error=NULL WHERE id=?",
                                (job_id,),
                            )
                    else:
                        # File is physically absent from disk:
                        has_genuine_evidence = (
                            cur_cstate == "completed"
                            and cur_cphase == "deleted"
                            and cur_deleted_at is not None
                            and cur_deleted_at != deliv_at
                        )
                        if not has_genuine_evidence:
                            # Absent file without genuine application deletion evidence!
                            # MUST be recorded as an explicit anomaly requiring investigation.
                            conn.execute(
                                "UPDATE delivery_jobs SET cleanup_state='failed', cleanup_phase='failed', "
                                "cleanup_error='HistoricalMissingSource', local_deleted_at=NULL WHERE id=?",
                                (job_id,),
                            )

                for statement in DELIVERY_SCHEMA.split(";"):
                    trimmed = statement.strip()
                    if trimmed.startswith("CREATE INDEX") or trimmed.startswith("CREATE UNIQUE INDEX"):
                        conn.execute(trimmed)

            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError("SQLite integrity check failed")
            conn.commit()
        return {"migration_required": needed, "applied": bool(apply and needed)}
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass
        if gate_holder is not None:
            release_migration_gate(db_path, gate_holder)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--uploads-dir", type=Path, default=None)
    args = parser.parse_args()
    result = reconcile(args.db, args.apply, uploads_dir=args.uploads_dir)
    print(f"MIGRATION_REQUIRED={str(result['migration_required']).lower()}")
    print(f"APPLIED={str(result['applied']).lower()}")
