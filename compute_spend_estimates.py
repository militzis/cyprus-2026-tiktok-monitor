"""compute_spend_estimates.py — populate estimated_spend_eur_{low,mid,high}
columns on tiktok_ads so the dashboard can show € figures instead of
opaque reach buckets ("10K-100K") that mean nothing to non-technical
readers.

Methodology (documented inline + in the dashboard caption):
  TikTok doesn't publish political-ad pricing (since their policy is
  political ads are banned globally). We anchor to TikTok's published
  EU/Cyprus COMMERCIAL CPM range of roughly €3-€8 per 1,000 impressions
  and compute three bounds per ad:

    low  = times_shown_lower_bound  × €3 / 1000   ← "at least"
    mid  = (lower + upper) / 2      × €5 / 1000   ← single-number estimate
    high = times_shown_upper_bound  × €8 / 1000   ← "could be as much as"

  All three are written to the DB so the dashboard can show whichever
  bound fits the headline. We deliberately store integers (rounded
  euros) because precision beyond that is meaningless for an estimate.

Sources for the CPM range:
  - TikTok Ads Manager pricing pages (commercial campaigns, EU 2024-2026)
  - Industry reports: Hootsuite, Sprout Social, eMarketer
  - Lower-end matches small-business CPMs in low-competition markets
  - Upper-end matches premium-audience / political-content CPMs in the
    US 2024 cycle (best available proxy)

Limitations / caveats (also surfaced in the dashboard caption):
  - TikTok's reach buckets are wide. A "10K-100K" ad could be 10K
    impressions or 99,999 — that's 10× difference in the bounds.
  - We don't know each ad's actual auction-clearing price.
  - Political content might run cheaper (oversupply, low competition
    from advertisers who follow TikTok's rules) OR more expensive
    (small audiences, narrow geo-targeting). We split the difference.

Idempotent — safe to re-run. Adds columns via ALTER TABLE if missing.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None

# CPM bounds in EUR (per 1,000 impressions)
CPM_LOW  = 3.0
CPM_MID  = 5.0
CPM_HIGH = 8.0

MASTER_DB = r'C:\Users\milit\meta_pipeline_data\politician_ads.db'
PUB_OD    = r'C:\Users\milit\OneDrive\Documents\META library content\politician_ads_public.db'
PUB_DP    = r'C:\Users\milit\dev\cyprus-2026-tiktok-monitor\politician_ads_public.db'

NEW_COLS = [
    ('estimated_spend_eur_low',  'INTEGER'),
    ('estimated_spend_eur_mid',  'INTEGER'),
    ('estimated_spend_eur_high', 'INTEGER'),
]


def ensure_schema(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(tiktok_ads)")}
    for col, typ in NEW_COLS:
        if col not in existing:
            conn.execute(f"ALTER TABLE tiktok_ads ADD COLUMN {col} {typ}")
            print(f"  + added column {col} {typ}")
    conn.commit()


def compute_for_db(db_path: str, only_null: bool = False) -> tuple[int, int, int]:
    """Compute spend estimates for every row with non-null reach bounds.

    Returns (n_updated, n_skipped_no_bounds, total_rows). With
    `only_null=True`, only rows where estimated_spend_eur_mid IS NULL
    are touched (useful for incremental runs after a sweep).
    """
    if not os.path.exists(db_path):
        print(f"  SKIP (missing): {db_path}")
        return (0, 0, 0)

    conn = sqlite3.connect(db_path)
    ensure_schema(conn)

    where = ""
    if only_null:
        where = "AND estimated_spend_eur_mid IS NULL"

    rows = conn.execute(f"""
        SELECT ad_id, times_shown_lower_bound, times_shown_upper_bound
        FROM tiktok_ads
        WHERE 1=1 {where}
    """).fetchall()

    n_updated = 0
    n_skipped = 0
    for ad_id, lo, hi in rows:
        if lo is None and hi is None:
            n_skipped += 1
            continue
        # Use the bound we have; if only one is present, treat it as both.
        lo = lo if lo is not None else hi
        hi = hi if hi is not None else lo
        mid = (lo + hi) / 2
        low_eur  = round(lo  * CPM_LOW  / 1000)
        mid_eur  = round(mid * CPM_MID  / 1000)
        high_eur = round(hi  * CPM_HIGH / 1000)
        conn.execute("""
            UPDATE tiktok_ads
               SET estimated_spend_eur_low  = ?,
                   estimated_spend_eur_mid  = ?,
                   estimated_spend_eur_high = ?
             WHERE ad_id = ?
        """, (low_eur, mid_eur, high_eur, ad_id))
        n_updated += 1
    conn.commit()

    # Quick sanity summary
    totals = conn.execute("""
        SELECT
          COALESCE(SUM(estimated_spend_eur_low),  0),
          COALESCE(SUM(estimated_spend_eur_mid),  0),
          COALESCE(SUM(estimated_spend_eur_high), 0)
        FROM tiktok_ads
    """).fetchone()
    conn.close()

    print(f"  ✓ {db_path.split(chr(92))[-1]}: "
          f"updated {n_updated}, skipped {n_skipped} (no bounds), "
          f"totals €{totals[0]:,} / €{totals[1]:,} / €{totals[2]:,}  (low/mid/high)")
    return (n_updated, n_skipped, len(rows))


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument('--only-null', action='store_true',
                   help='Skip rows where estimated_spend_eur_mid is already set (incremental mode).')
    p.add_argument('--master-only', action='store_true',
                   help='Only touch the master DB (skip both public DBs). Use when local-only.')
    p.add_argument('--db', default=None,
                   help='Override DB path. Overrides --master-only and the 3-DB sweep. '
                        'Useful in CI where POLITICIAN_ADS_DB env var points to deploy public DB.')
    args = p.parse_args()

    # CI/cron path: --db (or POLITICIAN_ADS_DB env var) overrides everything
    env_db = args.db or os.environ.get('POLITICIAN_ADS_DB')
    if env_db:
        print(f"  CPM bounds: low=€{CPM_LOW}/k  mid=€{CPM_MID}/k  high=€{CPM_HIGH}/k\n")
        compute_for_db(env_db, only_null=args.only_null)
        return

    print(f"  CPM bounds: low=€{CPM_LOW}/k  mid=€{CPM_MID}/k  high=€{CPM_HIGH}/k\n")
    targets = [MASTER_DB] if args.master_only else [MASTER_DB, PUB_OD, PUB_DP]
    for db in targets:
        compute_for_db(db, only_null=args.only_null)


if __name__ == '__main__':
    main()
