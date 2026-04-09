from __future__ import annotations

import argparse
import os
import sqlite3
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import psycopg2
from psycopg2.extras import execute_values

from db_store import DBStore
from runtime_paths import runtime_db_path


TABLES = ("slice_review", "mapping_review", "material_registry", "audit_log")


def _table_exists_sqlite(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute("select 1 from sqlite_master where type='table' and name=?", (table,))
    return cur.fetchone() is not None


def _fetch_rows(conn: sqlite3.Connection, table: str, columns: Sequence[str]) -> List[Tuple[Any, ...]]:
    if not _table_exists_sqlite(conn, table):
        return []
    sql = f"select {', '.join(columns)} from {table}"
    return [tuple(r) for r in conn.execute(sql).fetchall()]


def _group_by_tenant(rows: Iterable[Tuple[Any, ...]]) -> Dict[str, List[Tuple[Any, ...]]]:
    grouped: Dict[str, List[Tuple[Any, ...]]] = defaultdict(list)
    for row in rows:
        tenant_id = str(row[0])
        grouped[tenant_id].append(row)
    return grouped


def _set_tenant(cur: Any, tenant_id: str) -> None:
    cur.execute("select set_config('app.tenant_id', %s, true)", (tenant_id,))


def migrate(sqlite_path: str, target_database_url: str, dry_run: bool) -> None:
    DBStore(target_database_url)

    sqlite_conn = sqlite3.connect(sqlite_path)
    try:
        slice_rows = _fetch_rows(
            sqlite_conn,
            "slice_review",
            ("tenant_id", "slice_id", "review_status", "reviewer", "reviewed_at", "comment"),
        )
        mapping_rows = _fetch_rows(
            sqlite_conn,
            "mapping_review",
            (
                "tenant_id",
                "map_key",
                "confirm_status",
                "reviewer",
                "reviewed_at",
                "comment",
                "target_mother_question_id",
            ),
        )
        material_rows = _fetch_rows(
            sqlite_conn,
            "material_registry",
            (
                "tenant_id",
                "material_version_id",
                "file_path",
                "checksum",
                "status",
                "created_at",
                "effective_at",
            ),
        )
        audit_rows = _fetch_rows(
            sqlite_conn,
            "audit_log",
            (
                "id",
                "tenant_id",
                "actor",
                "action",
                "resource_type",
                "resource_id",
                "before_json",
                "after_json",
                "created_at",
            ),
        )
    finally:
        sqlite_conn.close()

    print("source_counts:")
    print(f"  slice_review={len(slice_rows)}")
    print(f"  mapping_review={len(mapping_rows)}")
    print(f"  material_registry={len(material_rows)}")
    print(f"  audit_log={len(audit_rows)}")

    if dry_run:
        print("dry_run=true, no data written to PostgreSQL.")
        return

    pg_conn = psycopg2.connect(target_database_url)
    try:
        with pg_conn:
            with pg_conn.cursor() as cur:
                for tenant_id, rows in _group_by_tenant(slice_rows).items():
                    _set_tenant(cur, tenant_id)
                    execute_values(
                        cur,
                        """
                        insert into slice_review
                        (tenant_id, slice_id, review_status, reviewer, reviewed_at, comment)
                        values %s
                        on conflict (tenant_id, slice_id) do update set
                          review_status=excluded.review_status,
                          reviewer=excluded.reviewer,
                          reviewed_at=excluded.reviewed_at,
                          comment=excluded.comment
                        """,
                        rows,
                    )

                for tenant_id, rows in _group_by_tenant(mapping_rows).items():
                    _set_tenant(cur, tenant_id)
                    execute_values(
                        cur,
                        """
                        insert into mapping_review
                        (tenant_id, map_key, confirm_status, reviewer, reviewed_at, comment, target_mother_question_id)
                        values %s
                        on conflict (tenant_id, map_key) do update set
                          confirm_status=excluded.confirm_status,
                          reviewer=excluded.reviewer,
                          reviewed_at=excluded.reviewed_at,
                          comment=excluded.comment,
                          target_mother_question_id=excluded.target_mother_question_id
                        """,
                        rows,
                    )

                for tenant_id, rows in _group_by_tenant(material_rows).items():
                    _set_tenant(cur, tenant_id)
                    execute_values(
                        cur,
                        """
                        insert into material_registry
                        (tenant_id, material_version_id, file_path, checksum, status, created_at, effective_at)
                        values %s
                        on conflict (tenant_id, material_version_id) do update set
                          file_path=excluded.file_path,
                          checksum=excluded.checksum,
                          status=excluded.status,
                          created_at=excluded.created_at,
                          effective_at=excluded.effective_at
                        """,
                        rows,
                    )

                for row in audit_rows:
                    tenant_id = str(row[1])
                    _set_tenant(cur, tenant_id)
                    cur.execute(
                        """
                        insert into audit_log
                        (id, tenant_id, actor, action, resource_type, resource_id, before_json, after_json, created_at)
                        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        on conflict (id) do update set
                          tenant_id=excluded.tenant_id,
                          actor=excluded.actor,
                          action=excluded.action,
                          resource_type=excluded.resource_type,
                          resource_id=excluded.resource_id,
                          before_json=excluded.before_json,
                          after_json=excluded.after_json,
                          created_at=excluded.created_at
                        """,
                        row,
                    )

                cur.execute(
                    """
                    select setval(
                      pg_get_serial_sequence('audit_log', 'id'),
                      coalesce((select max(id) from audit_log), 1),
                      true
                    )
                    """
                )

        print("migration_done=true")
    finally:
        pg_conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate SQLite admin_p0.db data to PostgreSQL.")
    parser.add_argument(
        "--sqlite-path",
        default=str(runtime_db_path()),
        help=f"Path to source sqlite db file. Default: {runtime_db_path()}",
    )
    parser.add_argument(
        "--target-database-url",
        default=os.getenv("DATABASE_URL", ""),
        help="Target PostgreSQL DATABASE_URL. Defaults to env DATABASE_URL.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print source row counts; do not write data.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    target_database_url = str(args.target_database_url or "").strip()
    if not target_database_url:
        raise SystemExit("target DATABASE_URL is required (env DATABASE_URL or --target-database-url).")
    if not (
        target_database_url.startswith("postgresql://") or target_database_url.startswith("postgres://")
    ):
        raise SystemExit("target DATABASE_URL must start with postgresql:// or postgres://")
    if not os.path.exists(args.sqlite_path):
        raise SystemExit(f"sqlite file not found: {args.sqlite_path}")
    migrate(sqlite_path=args.sqlite_path, target_database_url=target_database_url, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
