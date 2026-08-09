#!/usr/bin/env python3
"""
HC CDN Player — Database Migration Script
==========================================
Adds new columns to the 'jobs' table required by the concurrency
architecture overhaul.  Safe to run on an existing production database.

- Uses ALTER TABLE ... ADD COLUMN IF NOT EXISTS for PostgreSQL.
- Uses a Python-level existence check for SQLite (which doesn't support
  IF NOT EXISTS on ALTER TABLE).
- Never deletes or modifies existing data.

Usage:
    source venv/bin/activate
    python3 migrate_jobs.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from app import create_app
from app.models import db
from sqlalchemy import text  # SQLAlchemy 2.x requires text() for raw SQL

# New columns: (column_name, sqlite_type, pg_type, default_sql)
NEW_JOB_COLUMNS = [
    ('stage',              'VARCHAR(50)',        'VARCHAR(50)',        "'queued'"),
    ('current_variant',    'VARCHAR(50)',        'VARCHAR(50)',        'NULL'),
    ('variant_index',      'INTEGER',            'INTEGER',            '0'),
    ('variant_total',      'INTEGER',            'INTEGER',            '0'),
    ('speed',              'VARCHAR(20)',        'VARCHAR(20)',        'NULL'),
    ('eta_seconds',        'INTEGER',            'INTEGER',            'NULL'),
    ('elapsed_seconds',    'INTEGER',            'INTEGER',            'NULL'),
    ('source_duration',    'REAL',               'DOUBLE PRECISION',   '0.0'),
    ('bytes_received',     'INTEGER',            'BIGINT',             '0'),
    ('bytes_total',        'INTEGER',            'BIGINT',             '0'),
    ('cdn_bytes_uploaded', 'INTEGER',            'BIGINT',             '0'),
    ('cdn_bytes_total',    'INTEGER',            'BIGINT',             '0'),
    ('cancel_requested',   'INTEGER DEFAULT 0',  'BOOLEAN DEFAULT FALSE', 'FALSE'),
]


def _existing_columns_sqlite(conn) -> set:
    """Return column names on the jobs table using SQLite PRAGMA."""
    rows = conn.execute(text("PRAGMA table_info(jobs)")).fetchall()
    return {row[1] for row in rows}


def _existing_columns_pg(conn) -> set:
    """Return column names on the jobs table using information_schema."""
    rows = conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'jobs'"
    )).fetchall()
    return {row[0] for row in rows}


def run_migration():
    app = create_app()
    db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    is_sqlite = db_uri.startswith('sqlite')
    dialect = 'sqlite' if is_sqlite else 'postgresql'

    print(f"[Migration] Database dialect : {dialect}")
    print(f"[Migration] URI              : {db_uri[:80]}...")
    print()

    added = []
    skipped = []
    errors = []

    with app.app_context():
        # Use a raw DBAPI connection for DDL — avoids transaction complications
        # with SQLAlchemy 2.x autobegin.
        with db.engine.connect() as conn:
            # Discover existing columns
            if is_sqlite:
                existing = _existing_columns_sqlite(conn)
            else:
                existing = _existing_columns_pg(conn)

            print(f"[Migration] Existing job columns ({len(existing)}): {sorted(existing)}")
            print()

            for col_name, sqlite_type, pg_type, default_val in NEW_JOB_COLUMNS:
                if col_name in existing:
                    skipped.append(col_name)
                    print(f"[Migration]   SKIP  {col_name}  (already exists)")
                    continue

                if is_sqlite:
                    # SQLite: type includes DEFAULT inline
                    col_type = sqlite_type
                    if 'DEFAULT' not in col_type.upper():
                        sql = f"ALTER TABLE jobs ADD COLUMN {col_name} {col_type} DEFAULT {default_val}"
                    else:
                        sql = f"ALTER TABLE jobs ADD COLUMN {col_name} {col_type}"
                else:
                    col_type = pg_type
                    sql = (
                        f"ALTER TABLE jobs ADD COLUMN IF NOT EXISTS "
                        f"{col_name} {col_type} DEFAULT {default_val}"
                    )

                try:
                    conn.execute(text(sql))
                    conn.commit()
                    added.append(col_name)
                    print(f"[Migration]   ADD   {col_name}  ({col_type})")
                except Exception as e:
                    conn.rollback()
                    errors.append((col_name, str(e)))
                    print(f"[Migration]   ERROR {col_name}: {e}")

        print()
        print("=" * 60)
        print(f"[Migration] Added   : {added or ['(none — all already present)']}")
        print(f"[Migration] Skipped : {skipped or ['(none)']}")
        if errors:
            print(f"[Migration] Errors  : {errors}")
        print("=" * 60)
        print()

        # Sync any new SQLAlchemy model tables / indexes
        print("[Migration] Running db.create_all() to sync model schema...")
        db.create_all()
        print("[Migration] db.create_all() complete.")
        print()
        print("[Migration] Done. ✓")


if __name__ == '__main__':
    run_migration()
