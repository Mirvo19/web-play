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
    python migrate_jobs.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from app import create_app
from app.models import db

# New columns: (column_name, sql_type_sqlite, sql_type_pg, default_sql)
NEW_JOB_COLUMNS = [
    ('stage',              'VARCHAR(50)',  'VARCHAR(50)',  "'queued'"),
    ('current_variant',    'VARCHAR(50)',  'VARCHAR(50)',  'NULL'),
    ('variant_index',      'INTEGER',      'INTEGER',      '0'),
    ('variant_total',      'INTEGER',      'INTEGER',      '0'),
    ('speed',              'VARCHAR(20)',  'VARCHAR(20)',  'NULL'),
    ('eta_seconds',        'INTEGER',      'INTEGER',      'NULL'),
    ('elapsed_seconds',    'INTEGER',      'INTEGER',      'NULL'),
    ('source_duration',    'REAL',         'DOUBLE PRECISION', '0.0'),
    ('bytes_received',     'INTEGER',      'BIGINT',       '0'),
    ('bytes_total',        'INTEGER',      'BIGINT',       '0'),
    ('cdn_bytes_uploaded', 'INTEGER',      'BIGINT',       '0'),
    ('cdn_bytes_total',    'INTEGER',      'BIGINT',       '0'),
    ('cancel_requested',   'INTEGER',      'BOOLEAN',      'FALSE'),
]


def _existing_columns(conn, dialect: str) -> set:
    """Return the set of column names currently on the jobs table."""
    if dialect == 'sqlite':
        rows = conn.execute("PRAGMA table_info(jobs)").fetchall()
        return {row[1] for row in rows}
    else:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'jobs'"
        ).fetchall()
        return {row[0] for row in rows}


def run_migration():
    app = create_app()
    db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    is_sqlite = db_uri.startswith('sqlite')
    dialect = 'sqlite' if is_sqlite else 'postgresql'

    print(f"[Migration] Database: {dialect}")
    print(f"[Migration] URI: {db_uri[:60]}...")

    with app.app_context():
        conn = db.engine.connect()

        existing = _existing_columns(conn, dialect)
        print(f"[Migration] Existing job columns: {sorted(existing)}")

        added = []
        skipped = []

        for col_name, sqlite_type, pg_type, default_val in NEW_JOB_COLUMNS:
            if col_name in existing:
                skipped.append(col_name)
                continue

            col_type = sqlite_type if is_sqlite else pg_type

            if is_sqlite:
                # SQLite ALTER TABLE does not support IF NOT EXISTS,
                # so we check in Python first (done above).
                sql = (
                    f"ALTER TABLE jobs ADD COLUMN {col_name} {col_type} "
                    f"DEFAULT {default_val}"
                )
            else:
                sql = (
                    f"ALTER TABLE jobs ADD COLUMN IF NOT EXISTS {col_name} {col_type} "
                    f"DEFAULT {default_val}"
                )

            try:
                conn.execute(db.text(sql))
                if not is_sqlite:
                    conn.execute(db.text("COMMIT"))
                added.append(col_name)
                print(f"[Migration] + Added column: {col_name} {col_type} DEFAULT {default_val}")
            except Exception as e:
                print(f"[Migration] ! Error adding {col_name}: {e}")

        if is_sqlite:
            conn.execute(db.text("COMMIT"))

        conn.close()

        print()
        print(f"[Migration] Done.")
        print(f"  Added   : {added or ['(none)']}")
        print(f"  Skipped : {skipped or ['(none)']}")
        print()
        print("[Migration] Run db.create_all() to sync any remaining model changes.")

        # Also run create_all to pick up any new tables or indexes
        db.create_all()
        print("[Migration] db.create_all() complete.")


if __name__ == '__main__':
    run_migration()
