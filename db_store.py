from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/admin_p0.db")


class DBStore:
    def __init__(self, database_url: str = DATABASE_URL):
        self.database_url = database_url
        self.is_postgres = database_url.startswith("postgresql://") or database_url.startswith("postgres://")
        if self.is_postgres:
            import psycopg2  # type: ignore

            self._pg = psycopg2
        else:
            path = database_url.replace("sqlite:///", "", 1)
            db_path = Path(path)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self.sqlite_path = str(db_path)
        self.init_db()

    @contextmanager
    def connect(self) -> Iterator[Any]:
        if self.is_postgres:
            conn = self._pg.connect(self.database_url)
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()
        else:
            conn = sqlite3.connect(self.sqlite_path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def init_db(self) -> None:
        with self.connect() as conn:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute(
                    """
                    create table if not exists slice_review (
                      tenant_id text not null,
                      slice_id integer not null,
                      review_status text not null,
                      reviewer text not null,
                      reviewed_at timestamptz not null,
                      comment text not null default '',
                      primary key (tenant_id, slice_id)
                    )
                    """
                )
                cur.execute(
                    """
                    create table if not exists mapping_review (
                      tenant_id text not null,
                      map_key text not null,
                      confirm_status text not null,
                      reviewer text not null,
                      reviewed_at timestamptz not null,
                      comment text not null default '',
                      target_mother_question_id text not null default '',
                      primary key (tenant_id, map_key)
                    )
                    """
                )
                cur.execute(
                    """
                    create table if not exists audit_log (
                      id bigserial primary key,
                      tenant_id text not null,
                      actor text not null,
                      action text not null,
                      resource_type text not null,
                      resource_id text not null,
                      before_json text not null default '{}',
                      after_json text not null default '{}',
                      created_at timestamptz not null
                    )
                    """
                )
                cur.execute(
                    """
                    create table if not exists material_registry (
                      tenant_id text not null,
                      material_version_id text not null,
                      file_path text not null,
                      checksum text not null,
                      status text not null,
                      created_at timestamptz not null,
                      effective_at timestamptz,
                      primary key (tenant_id, material_version_id)
                    )
                    """
                )
                # PostgreSQL RLS policies for tenant isolation.
                for table in ("slice_review", "mapping_review", "audit_log", "material_registry"):
                    cur.execute(f"alter table {table} enable row level security")
                    cur.execute(
                        f"""
                        do $$
                        begin
                          if not exists (
                            select 1 from pg_policies
                            where schemaname = current_schema()
                              and tablename = '{table}'
                              and policyname = 'tenant_isolation_{table}'
                          ) then
                            create policy tenant_isolation_{table}
                            on {table}
                            using (tenant_id = current_setting('app.tenant_id', true))
                            with check (tenant_id = current_setting('app.tenant_id', true));
                          end if;
                        end $$;
                        """
                    )
            else:
                cur.execute(
                    """
                    create table if not exists slice_review (
                      tenant_id text not null,
                      slice_id integer not null,
                      review_status text not null,
                      reviewer text not null,
                      reviewed_at text not null,
                      comment text not null default '',
                      primary key (tenant_id, slice_id)
                    )
                    """
                )
                cur.execute(
                    """
                    create table if not exists mapping_review (
                      tenant_id text not null,
                      map_key text not null,
                      confirm_status text not null,
                      reviewer text not null,
                      reviewed_at text not null,
                      comment text not null default '',
                      target_mother_question_id text not null default '',
                      primary key (tenant_id, map_key)
                    )
                    """
                )
                cur.execute(
                    """
                    create table if not exists audit_log (
                      id integer primary key autoincrement,
                      tenant_id text not null,
                      actor text not null,
                      action text not null,
                      resource_type text not null,
                      resource_id text not null,
                      before_json text not null default '{}',
                      after_json text not null default '{}',
                      created_at text not null
                    )
                    """
                )
                cur.execute(
                    """
                    create table if not exists material_registry (
                      tenant_id text not null,
                      material_version_id text not null,
                      file_path text not null,
                      checksum text not null,
                      status text not null,
                      created_at text not null,
                      effective_at text,
                      primary key (tenant_id, material_version_id)
                    )
                    """
                )

    def _set_rls_tenant(self, cur: Any, tenant_id: str) -> None:
        if self.is_postgres:
            cur.execute("select set_config('app.tenant_id', %s, true)", (tenant_id,))

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def upsert_slice_review(self, tenant_id: str, slice_id: int, review_status: str, reviewer: str, comment: str) -> dict:
        reviewed_at = self._now()
        with self.connect() as conn:
            cur = conn.cursor()
            self._set_rls_tenant(cur, tenant_id)
            if self.is_postgres:
                cur.execute(
                    """
                    insert into slice_review (tenant_id, slice_id, review_status, reviewer, reviewed_at, comment)
                    values (%s, %s, %s, %s, %s, %s)
                    on conflict (tenant_id, slice_id)
                    do update set review_status=excluded.review_status,
                                  reviewer=excluded.reviewer,
                                  reviewed_at=excluded.reviewed_at,
                                  comment=excluded.comment
                    """,
                    (tenant_id, slice_id, review_status, reviewer, reviewed_at, comment),
                )
            else:
                cur.execute(
                    """
                    insert into slice_review (tenant_id, slice_id, review_status, reviewer, reviewed_at, comment)
                    values (?, ?, ?, ?, ?, ?)
                    on conflict(tenant_id, slice_id)
                    do update set review_status=excluded.review_status,
                                  reviewer=excluded.reviewer,
                                  reviewed_at=excluded.reviewed_at,
                                  comment=excluded.comment
                    """,
                    (tenant_id, slice_id, review_status, reviewer, reviewed_at, comment),
                )
        return {
            "review_status": review_status,
            "reviewer": reviewer,
            "reviewed_at": reviewed_at,
            "comment": comment,
        }

    def load_slice_review(self, tenant_id: str) -> Dict[str, dict]:
        with self.connect() as conn:
            cur = conn.cursor()
            self._set_rls_tenant(cur, tenant_id)
            if self.is_postgres:
                cur.execute(
                    "select slice_id, review_status, reviewer, reviewed_at, comment from slice_review where tenant_id=%s",
                    (tenant_id,),
                )
            else:
                cur.execute(
                    "select slice_id, review_status, reviewer, reviewed_at, comment from slice_review where tenant_id=?",
                    (tenant_id,),
                )
            rows = cur.fetchall()
        out: Dict[str, dict] = {}
        for r in rows:
            if self.is_postgres:
                sid, status, reviewer, reviewed_at, comment = r
            else:
                sid, status, reviewer, reviewed_at, comment = r[0], r[1], r[2], r[3], r[4]
            out[str(sid)] = {
                "review_status": status,
                "reviewer": reviewer,
                "reviewed_at": str(reviewed_at),
                "comment": comment or "",
            }
        return out

    def upsert_mapping_review(
        self,
        tenant_id: str,
        map_key: str,
        confirm_status: str,
        reviewer: str,
        comment: str,
        target_mother_question_id: str,
    ) -> dict:
        reviewed_at = self._now()
        with self.connect() as conn:
            cur = conn.cursor()
            self._set_rls_tenant(cur, tenant_id)
            if self.is_postgres:
                cur.execute(
                    """
                    insert into mapping_review
                    (tenant_id, map_key, confirm_status, reviewer, reviewed_at, comment, target_mother_question_id)
                    values (%s, %s, %s, %s, %s, %s, %s)
                    on conflict (tenant_id, map_key)
                    do update set confirm_status=excluded.confirm_status,
                                  reviewer=excluded.reviewer,
                                  reviewed_at=excluded.reviewed_at,
                                  comment=excluded.comment,
                                  target_mother_question_id=excluded.target_mother_question_id
                    """,
                    (tenant_id, map_key, confirm_status, reviewer, reviewed_at, comment, target_mother_question_id),
                )
            else:
                cur.execute(
                    """
                    insert into mapping_review
                    (tenant_id, map_key, confirm_status, reviewer, reviewed_at, comment, target_mother_question_id)
                    values (?, ?, ?, ?, ?, ?, ?)
                    on conflict(tenant_id, map_key)
                    do update set confirm_status=excluded.confirm_status,
                                  reviewer=excluded.reviewer,
                                  reviewed_at=excluded.reviewed_at,
                                  comment=excluded.comment,
                                  target_mother_question_id=excluded.target_mother_question_id
                    """,
                    (tenant_id, map_key, confirm_status, reviewer, reviewed_at, comment, target_mother_question_id),
                )
        return {
            "confirm_status": confirm_status,
            "reviewer": reviewer,
            "reviewed_at": reviewed_at,
            "comment": comment,
            "target_mother_question_id": target_mother_question_id,
        }

    def load_mapping_review(self, tenant_id: str) -> Dict[str, dict]:
        with self.connect() as conn:
            cur = conn.cursor()
            self._set_rls_tenant(cur, tenant_id)
            if self.is_postgres:
                cur.execute(
                    "select map_key, confirm_status, reviewer, reviewed_at, comment, target_mother_question_id from mapping_review where tenant_id=%s",
                    (tenant_id,),
                )
            else:
                cur.execute(
                    "select map_key, confirm_status, reviewer, reviewed_at, comment, target_mother_question_id from mapping_review where tenant_id=?",
                    (tenant_id,),
                )
            rows = cur.fetchall()
        out: Dict[str, dict] = {}
        for r in rows:
            if self.is_postgres:
                map_key, status, reviewer, reviewed_at, comment, target = r
            else:
                map_key, status, reviewer, reviewed_at, comment, target = r[0], r[1], r[2], r[3], r[4], r[5]
            out[str(map_key)] = {
                "confirm_status": status,
                "reviewer": reviewer,
                "reviewed_at": str(reviewed_at),
                "comment": comment or "",
                "target_mother_question_id": target or "",
            }
        return out

    def write_audit_log(
        self,
        tenant_id: str,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str,
        before_json: str,
        after_json: str,
    ) -> None:
        created_at = self._now()
        with self.connect() as conn:
            cur = conn.cursor()
            self._set_rls_tenant(cur, tenant_id)
            if self.is_postgres:
                cur.execute(
                    """
                    insert into audit_log
                    (tenant_id, actor, action, resource_type, resource_id, before_json, after_json, created_at)
                    values (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (tenant_id, actor, action, resource_type, resource_id, before_json, after_json, created_at),
                )
            else:
                cur.execute(
                    """
                    insert into audit_log
                    (tenant_id, actor, action, resource_type, resource_id, before_json, after_json, created_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (tenant_id, actor, action, resource_type, resource_id, before_json, after_json, created_at),
                )

    def list_material_versions(self, tenant_id: str) -> List[dict]:
        with self.connect() as conn:
            cur = conn.cursor()
            self._set_rls_tenant(cur, tenant_id)
            if self.is_postgres:
                cur.execute(
                    """
                    select material_version_id, file_path, checksum, status, created_at, effective_at
                    from material_registry where tenant_id=%s order by created_at desc
                    """,
                    (tenant_id,),
                )
            else:
                cur.execute(
                    """
                    select material_version_id, file_path, checksum, status, created_at, effective_at
                    from material_registry where tenant_id=? order by created_at desc
                    """,
                    (tenant_id,),
                )
            rows = cur.fetchall()
        out = []
        for r in rows:
            if self.is_postgres:
                mv_id, file_path, checksum, status, created_at, effective_at = r
            else:
                mv_id, file_path, checksum, status, created_at, effective_at = r[0], r[1], r[2], r[3], r[4], r[5]
            out.append(
                {
                    "material_version_id": mv_id,
                    "file_path": file_path,
                    "checksum": checksum,
                    "status": status,
                    "created_at": str(created_at),
                    "effective_at": str(effective_at) if effective_at else None,
                }
            )
        return out

    def register_material_version(
        self,
        tenant_id: str,
        material_version_id: str,
        file_path: str,
        checksum: str,
        status: str = "ready_for_review",
    ) -> dict:
        created_at = self._now()
        with self.connect() as conn:
            cur = conn.cursor()
            self._set_rls_tenant(cur, tenant_id)
            if self.is_postgres:
                cur.execute(
                    """
                    insert into material_registry
                    (tenant_id, material_version_id, file_path, checksum, status, created_at, effective_at)
                    values (%s, %s, %s, %s, %s, %s, %s)
                    on conflict (tenant_id, material_version_id)
                    do update set file_path=excluded.file_path,
                                  checksum=excluded.checksum,
                                  status=excluded.status
                    """,
                    (tenant_id, material_version_id, file_path, checksum, status, created_at, None),
                )
            else:
                cur.execute(
                    """
                    insert into material_registry
                    (tenant_id, material_version_id, file_path, checksum, status, created_at, effective_at)
                    values (?, ?, ?, ?, ?, ?, ?)
                    on conflict(tenant_id, material_version_id)
                    do update set file_path=excluded.file_path,
                                  checksum=excluded.checksum,
                                  status=excluded.status
                    """,
                    (tenant_id, material_version_id, file_path, checksum, status, created_at, None),
                )
        return {
            "material_version_id": material_version_id,
            "file_path": file_path,
            "checksum": checksum,
            "status": status,
            "created_at": created_at,
            "effective_at": None,
        }

    def set_effective_material_version(self, tenant_id: str, material_version_id: str) -> Optional[dict]:
        now = self._now()
        with self.connect() as conn:
            cur = conn.cursor()
            self._set_rls_tenant(cur, tenant_id)
            if self.is_postgres:
                cur.execute(
                    """
                    update material_registry
                    set status='effective', effective_at=%s
                    where tenant_id=%s and material_version_id=%s
                    returning material_version_id, file_path, checksum, status, created_at, effective_at
                    """,
                    (now, tenant_id, material_version_id),
                )
                row = cur.fetchone()
            else:
                cur.execute(
                    """
                    update material_registry
                    set status='effective', effective_at=?
                    where tenant_id=? and material_version_id=?
                    """,
                    (now, tenant_id, material_version_id),
                )
                cur.execute(
                    """
                    select material_version_id, file_path, checksum, status, created_at, effective_at
                    from material_registry where tenant_id=? and material_version_id=?
                    """,
                    (tenant_id, material_version_id),
                )
                row = cur.fetchone()
        if not row:
            return None
        if self.is_postgres:
            mv_id, file_path, checksum, status, created_at, effective_at = row
        else:
            mv_id, file_path, checksum, status, created_at, effective_at = row[0], row[1], row[2], row[3], row[4], row[5]
        return {
            "material_version_id": mv_id,
            "file_path": file_path,
            "checksum": checksum,
            "status": status,
            "created_at": str(created_at),
            "effective_at": str(effective_at) if effective_at else None,
        }

    def archive_material_version(self, tenant_id: str, material_version_id: str) -> Optional[dict]:
        with self.connect() as conn:
            cur = conn.cursor()
            self._set_rls_tenant(cur, tenant_id)
            if self.is_postgres:
                cur.execute(
                    """
                    update material_registry
                    set status='archived'
                    where tenant_id=%s and material_version_id=%s
                    returning material_version_id, file_path, checksum, status, created_at, effective_at
                    """,
                    (tenant_id, material_version_id),
                )
                row = cur.fetchone()
            else:
                cur.execute(
                    """
                    update material_registry
                    set status='archived'
                    where tenant_id=? and material_version_id=?
                    """,
                    (tenant_id, material_version_id),
                )
                cur.execute(
                    """
                    select material_version_id, file_path, checksum, status, created_at, effective_at
                    from material_registry where tenant_id=? and material_version_id=?
                    """,
                    (tenant_id, material_version_id),
                )
                row = cur.fetchone()
        if not row:
            return None
        if self.is_postgres:
            mv_id, file_path, checksum, status, created_at, effective_at = row
        else:
            mv_id, file_path, checksum, status, created_at, effective_at = row[0], row[1], row[2], row[3], row[4], row[5]
        return {
            "material_version_id": mv_id,
            "file_path": file_path,
            "checksum": checksum,
            "status": status,
            "created_at": str(created_at),
            "effective_at": str(effective_at) if effective_at else None,
        }

    def delete_material_version(self, tenant_id: str, material_version_id: str) -> bool:
        with self.connect() as conn:
            cur = conn.cursor()
            self._set_rls_tenant(cur, tenant_id)
            if self.is_postgres:
                cur.execute(
                    "delete from material_registry where tenant_id=%s and material_version_id=%s",
                    (tenant_id, material_version_id),
                )
                return cur.rowcount > 0
            cur.execute(
                "delete from material_registry where tenant_id=? and material_version_id=?",
                (tenant_id, material_version_id),
            )
            return cur.rowcount > 0


_store: Optional[DBStore] = None


def get_store() -> DBStore:
    global _store
    if _store is None:
        _store = DBStore()
    return _store
