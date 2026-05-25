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

import sqlite3
from datetime import datetime, timezone


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create pipeline_health table if it doesn't exist. Idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_health (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_kind     TEXT    NOT NULL,
            started_at   TEXT    NOT NULL,
            finished_at  TEXT    NOT NULL,
            status       TEXT    NOT NULL,
            ads_checked  INTEGER,
            changes      INTEGER,
            errors       INTEGER,
            error_msg    TEXT,
            since_arg    TEXT,
            limit_arg    INTEGER
        )
    """)
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
    must never take down the pipeline run that called it."""
    try:
        conn = sqlite3.connect(db_path)
        ensure_schema(conn)
        conn.execute("""
            INSERT INTO pipeline_health
              (run_kind, started_at, finished_at, status,
               ads_checked, changes, errors, error_msg, since_arg, limit_arg)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  ⚠ heartbeat write failed: {e!r}", flush=True)
