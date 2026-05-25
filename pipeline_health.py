"""pipeline_health.py — shared pipeline heartbeat for all TikTok pipeline scripts.

Single source of truth for the pipeline_health table schema and record() function.
Previously duplicated verbatim across:
  - discover_tiktok_ads.py
  - discover_content_keywords.py
  - refresh_known_catalogs.py
  - tiktok_tier2_fetch.py

Usage:
    import pipeline_health
    pipeline_health.record(DB_PATH, run_kind='catalog_refresh', started_at=..., status='ok')
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create pipeline_health table if it doesn't exist. Idempotent.
    Also adds workflow_source column (ALTER TABLE) if missing on older DBs."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_health (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_kind        TEXT    NOT NULL,
            started_at      TEXT    NOT NULL,
            finished_at     TEXT    NOT NULL,
            status          TEXT    NOT NULL,
            ads_checked     INTEGER,
            changes         INTEGER,
            errors          INTEGER,
            error_msg       TEXT,
            since_arg       TEXT,
            limit_arg       INTEGER,
            workflow_source TEXT
        )
    """)
    # Migrate existing DBs that predate the workflow_source column.
    existing = {r[1] for r in conn.execute("PRAGMA table_info(pipeline_health)")}
    if 'workflow_source' not in existing:
        conn.execute("ALTER TABLE pipeline_health ADD COLUMN workflow_source TEXT")
    conn.commit()


def record(
    db_path: str,
    run_kind: str,
    started_at: str,
    status: str,
    ads_checked: int = 0,
    changes: int = 0,
    errors: int = 0,
    error_msg: str | None = None,
    since_arg: str | None = None,
    limit_arg: int | None = None,
) -> None:
    """Best-effort heartbeat write. Never raises — a heartbeat failure
    must never take down the pipeline run that called it.

    workflow_source is auto-populated from the GITHUB_WORKFLOW env var so
    callers don't need to change their call sites. On local dev it's NULL.
    This lets the dashboard distinguish daily vs election-week vs weekly runs
    even though they all write the same run_kind='catalog_refresh'.
    """
    workflow_source = os.environ.get('GITHUB_WORKFLOW') or None
    try:
        conn = sqlite3.connect(db_path)
        ensure_schema(conn)
        conn.execute("""
            INSERT INTO pipeline_health
              (run_kind, started_at, finished_at, status,
               ads_checked, changes, errors, error_msg,
               since_arg, limit_arg, workflow_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_kind,
            started_at,
            datetime.now(timezone.utc).isoformat(),
            status,
            ads_checked,
            changes,
            errors,
            error_msg,
            since_arg,
            limit_arg,
            workflow_source,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  ⚠ heartbeat write failed: {e!r}", flush=True)
