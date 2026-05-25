"""strip_public_db.py — apply the public-snapshot filter in-place on
whatever DB POLITICIAN_ADS_DB points at.

Mirrors build_public_db.py's DELETE logic but operates IN-PLACE rather
than building a fresh DB from master. Designed to run from GitHub Actions
workflows AFTER discovery + enrichment write to the public DB but BEFORE
the workflow's commit + push step. Without this, content_keyword rows
that the cron's keyword sweep discovers (but no human has triaged) end
up on the public dashboard, as happened on 2026-05-20 when 483 unverified
keyword hits — including obvious false positives like 'HERMES DIGITAL OU'
(Estonian ad agency) and several numeric-handle funder-ID quirks —
appeared on the live dashboard.

What gets dropped (matches build_public_db.py exactly):
  - match_type LIKE 'content_keyword'  / 'content_keyword%'
  - match_type LIKE 'likely_false_positive%'
  - advertiser_disclosed_name GLOB '[0-9]*'  (funder-ID quirk)

What gets NULL'd (size-only, columns unused by the dashboard):
  - targeting_json, avatar_url
  (reach_by_country is KEPT — dashboard uses it for per-country breakdowns)

Idempotent. Best-effort: any error is logged but never raises, so a
cleanup failure cannot block the workflow's commit + push step.

Usage:
  POLITICIAN_ADS_DB=politician_ads_public.db python strip_public_db.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

DB = os.environ.get('POLITICIAN_ADS_DB',
                    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 'politician_ads_public.db'))

# Single source of truth for "what counts as public" lives in
# build_public_db.py; this script keeps the patterns in lockstep.
DROP_MATCH_LIKE          = ['content_keyword%',      # covers exact + prefixed variants
                            'likely_false_positive%']
DROP_NUMERIC_HANDLE_GLOB = '[0-9]*'
COLUMNS_TO_NULL          = ['targeting_json', 'avatar_url']


def main() -> int:
    if not os.path.exists(DB):
        print(f"  ⚠ strip_public_db: {DB} not found — nothing to do.")
        return 0

    try:
        conn = sqlite3.connect(DB)
        # If tiktok_ads doesn't exist (fresh DB, discovery hasn't run yet),
        # we have nothing to filter — exit cleanly so the workflow continues.
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tiktok_ads'"
        ).fetchone()
        if not exists:
            print(f"  ⚠ strip_public_db: no tiktok_ads table — skipping.")
            conn.close()
            return 0

        before = conn.execute("SELECT COUNT(*) FROM tiktok_ads").fetchone()[0]

        n_dropped = 0
        for pattern in DROP_MATCH_LIKE:
            n_dropped += conn.execute(
                "DELETE FROM tiktok_ads WHERE match_type LIKE ?", (pattern,)
            ).rowcount
        n_dropped += conn.execute(
            "DELETE FROM tiktok_ads WHERE advertiser_disclosed_name GLOB ?",
            (DROP_NUMERIC_HANDLE_GLOB,)
        ).rowcount

        for col in COLUMNS_TO_NULL:
            try:
                conn.execute(
                    f"UPDATE tiktok_ads SET {col}=NULL WHERE {col} IS NOT NULL"
                )
            except sqlite3.OperationalError:
                pass  # column doesn't exist on older schema — fine

        conn.commit()
        # VACUUM has to live outside a transaction.
        conn.execute("VACUUM")
        after = conn.execute("SELECT COUNT(*) FROM tiktok_ads").fetchone()[0]
        conn.close()

        print(f"  ✓ strip_public_db: {before} → {after} ads "
              f"({n_dropped} non-public rows dropped)", flush=True)
        return 0
    except Exception as e:
        # Best-effort: don't block the workflow's commit + push step on a
        # cleanup failure. Worst case the dashboard temporarily shows some
        # content_keyword rows; better than blocking a fresh refresh +
        # takedown data from reaching production.
        print(f"  ⚠ strip_public_db failed (non-fatal): {e!r}",
              file=sys.stderr, flush=True)
        return 0


if __name__ == '__main__':
    sys.exit(main())
