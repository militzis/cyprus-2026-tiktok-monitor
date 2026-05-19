"""Tier 2 — call /v2/research/adlib/ad/detail/ for each saved ad and
capture richer metadata that the basic /ad/query/ endpoint doesn't return:

  - audience_targeting    targeting spec the advertiser used
  - age + gender          demographic distribution of who saw the ad
  - interest              interest categories TikTok matched the ad to
  - follower_count        advertiser's follower count at time of fetch
  - profile_url + avatar  visual + canonical link
  - unique_users_seen_by_country   per-country reach breakdown
  - number_of_users_targeted        audience size the advertiser bought

Each ad gets the raw JSON response in targeting_json + a few high-value
fields extracted into dedicated columns for easy querying.

Idempotent — only fetches ads where targeting_json IS NULL (default) OR
where the existing targeting record is older than --since-days. Cron-safe.

Usage:
    python tiktok_tier2_fetch.py                  # enrich every unenriched ad
    python tiktok_tier2_fetch.py --limit 500      # cap per run (cron friendly)
    python tiktok_tier2_fetch.py --since-days 30  # also re-enrich ads
                                                  # whose data is >30 days old
    python tiktok_tier2_fetch.py --dry-run        # preview, no DB writes
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import json
import time
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None

BASE = os.path.dirname(os.path.abspath(__file__))
# Match the canonical default used by discover_tiktok_ads + refresh_ad_statuses:
# the master DB lives OUTSIDE OneDrive to avoid sync-conflict rollbacks.
# In the cron the env var is set to politician_ads_public.db instead.
DB   = os.environ.get(
    'POLITICIAN_ADS_DB',
    r'C:\Users\milit\meta_pipeline_data\politician_ads.db',
)
sys.path.insert(0, BASE)
import discover_tiktok_ads as t

AD_DETAIL_URL = "https://open.tiktokapis.com/v2/research/adlib/ad/detail/"

FIELDS = ",".join([
    "country", "audience_targeting", "video_interactions",
    "creator_interactions", "interest",
    "age", "gender",
    "business_id", "business_name", "country_code", "paid_by",
    "follower_count", "avatar_url", "profile_url",
    "unique_users_seen", "unique_users_seen_by_country",
    "number_of_users_targeted",
])

NEW_COLS = [
    ("targeting_age",     "TEXT"),   # JSON-stringified {"18-24":True, ...}
    ("targeting_gender",  "TEXT"),
    ("targeting_country", "TEXT"),
    ("targeting_interest","TEXT"),
    ("targeting_audience","TEXT"),
    ("follower_count",    "INTEGER"),
    ("profile_url",       "TEXT"),
    ("avatar_url",        "TEXT"),
    ("number_of_users_targeted", "TEXT"),
    ("reach_by_country",  "TEXT"),   # JSON {"CY":"10K-100K",...}
    ("targeting_fetched_at", "TEXT"),  # ISO timestamp of when we last enriched
]


def ensure_schema(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(tiktok_ads)")}
    for col, typ in NEW_COLS:
        if col not in existing:
            conn.execute(f"ALTER TABLE tiktok_ads ADD COLUMN {col} {typ}")
            print(f"  + added column {col} {typ}")
    # Heartbeat table — shared with refresh_ad_statuses.py
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


def record_health(conn, started_at, status, n_done, n_errors,
                  error_msg=None, since_arg=None, limit_arg=None):
    """Insert one row into pipeline_health. Always called from a top-level
    try/finally so even crashes get recorded."""
    finished_at = datetime.utcnow().isoformat()
    conn.execute("""
        INSERT INTO pipeline_health
          (run_kind, started_at, finished_at, status,
           ads_checked, changes, errors, error_msg, since_arg, limit_arg)
        VALUES ('enrich', ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (started_at, finished_at, status, n_done, n_done, n_errors,
          error_msg, since_arg, limit_arg))
    conn.commit()


def select_candidates(conn, since_days: int | None, limit: int | None) -> list[tuple]:
    """Pick the ads to enrich on this run.

      - Default: ads where targeting_json IS NULL (never enriched)
      - With --since-days N: ALSO re-enrich ads whose targeting_fetched_at
        is older than N days (catches advertisers whose targeting/audience
        evolved over time)
    """
    if since_days is not None and since_days > 0:
        sql = """
          SELECT ad_id, advertiser_disclosed_name
          FROM tiktok_ads
          WHERE targeting_json IS NULL OR targeting_json = ''
             OR targeting_fetched_at IS NULL
             OR julianday('now') - julianday(targeting_fetched_at) > ?
          ORDER BY advertiser_disclosed_name, first_shown
        """
        params = (since_days,)
    else:
        sql = """
          SELECT ad_id, advertiser_disclosed_name
          FROM tiktok_ads
          WHERE targeting_json IS NULL OR targeting_json = ''
          ORDER BY advertiser_disclosed_name, first_shown
        """
        params = ()
    rows = conn.execute(sql, params).fetchall()
    if limit:
        rows = rows[:limit]
    return rows


def enrich_one(conn, ad_id: str) -> bool:
    """Fetch detail for one ad and write to DB. Returns True on success."""
    try:
        data = t._api_post(AD_DETAIL_URL, {"fields": FIELDS}, {"ad_id": int(ad_id)})
    except t.RateLimitExceeded:
        raise
    except Exception as e:
        print(f"  ✗ ad {ad_id}: {e}")
        return False
    detail = data.get("data", {}) or {}
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        UPDATE tiktok_ads SET
            targeting_json     = ?,
            targeting_age      = ?,
            targeting_gender   = ?,
            targeting_country  = ?,
            targeting_interest = ?,
            targeting_audience = ?,
            follower_count     = ?,
            profile_url        = ?,
            avatar_url         = ?,
            number_of_users_targeted = ?,
            reach_by_country   = ?,
            targeting_fetched_at = ?
        WHERE ad_id = ?
    """, (
        json.dumps(detail, ensure_ascii=False),
        json.dumps(detail.get("age") or {}, ensure_ascii=False),
        json.dumps(detail.get("gender") or {}, ensure_ascii=False),
        json.dumps(detail.get("country") or [], ensure_ascii=False),
        detail.get("interest"),
        detail.get("audience_targeting"),
        detail.get("follower_count"),
        detail.get("profile_url"),
        detail.get("avatar_url"),
        detail.get("number_of_users_targeted"),
        json.dumps(detail.get("unique_users_seen_by_country") or {}, ensure_ascii=False),
        now,
        ad_id,
    ))
    conn.commit()
    return True


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument('--limit',       type=int, default=None,
                   help='Cap how many ads to enrich this run (cron friendly).')
    p.add_argument('--since-days',  type=int, default=None,
                   help='ALSO re-enrich ads whose targeting was fetched > N days ago.')
    p.add_argument('--dry-run',     action='store_true',
                   help="List what we'd fetch, then exit without writing.")
    args = p.parse_args()

    if not t.CLIENT_KEY or not t.CLIENT_SECRET:
        sys.exit("ERROR: TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET missing from .env")

    # Acquire exclusive write lock — same protocol as refresh_ad_statuses.py.
    # Prevents two concurrent enrich runs from racing on the same row.
    from db_lock import db_lock
    with db_lock(DB):
        _run(args)


def _run(args):
    started_at = datetime.utcnow().isoformat()
    conn = sqlite3.connect(DB)
    ensure_schema(conn)

    rows = select_candidates(conn, args.since_days, args.limit)
    print(f"  Enriching {len(rows)} ads (DB: {DB})\n")
    if args.dry_run:
        for r in rows[:20]:
            print(f"    {r[0]}  @{r[1] or '?'}")
        if len(rows) > 20:
            print(f"    … and {len(rows) - 20} more")
        print("  (--dry-run: no DB writes)")
        conn.close()
        return

    if not rows:
        record_health(conn, started_at, 'ok', 0, 0,
                      since_arg=str(args.since_days) if args.since_days else None,
                      limit_arg=args.limit)
        conn.close()
        print("  Nothing to enrich.")
        return

    t.get_access_token()
    n_done, n_errors, crash_msg = 0, 0, None
    try:
        for i, (ad_id, handle) in enumerate(rows, 1):
            try:
                ok = enrich_one(conn, ad_id)
                if ok:
                    n_done += 1
                else:
                    n_errors += 1
            except t.RateLimitExceeded:
                print(f"  [429] hit quota at ad {i}/{len(rows)} — stopping.")
                break
            if i % 25 == 0:
                print(f"  ... {i}/{len(rows)} processed (ok: {n_done}, err: {n_errors})")
            time.sleep(getattr(t, 'REQUEST_DELAY', 0.5))
    except Exception as e:
        crash_msg = repr(e)
        raise
    finally:
        record_health(
            conn, started_at,
            status='failed' if crash_msg else 'ok',
            n_done=n_done, n_errors=n_errors, error_msg=crash_msg,
            since_arg=str(args.since_days) if args.since_days else None,
            limit_arg=args.limit,
        )
        conn.close()
        print(f"\n  ✓ enriched {n_done}/{len(rows)} ads ({n_errors} errors)")


if __name__ == '__main__':
    main()
