"""check_ad_library_status.py — Use a headless browser to check the TikTok Ad
Library page for each 'disappeared' ad and determine if it was:

  - enforcement:  "removed from TikTok due to a violation of TikTok's terms"
  - voluntary:    "Campaign ended — advertiser stopped voluntarily"
  - unknown:      page loaded but couldn't determine status

Updates the DB status_statement in-place. Idempotent — skips ads already
classified as enforcement or voluntary (only re-checks 'No longer returned').

Usage:
    python check_ad_library_status.py                  # check all unclassified
    python check_ad_library_status.py --dry-run        # print URLs, no DB writes
    python check_ad_library_status.py --limit 20       # cap per run
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None

# Honour POLITICIAN_ADS_DB so CI (which only has the public DB) uses the right
# file. Without this the script opens a fresh empty SQLite at a hardcoded
# Windows path that doesn't exist on Ubuntu runners, finds 0 rows, and exits
# silently — making the CI step a no-op (bug discovered 2026-05-25).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline_health as _ph
from db_lock import db_lock

DB = os.environ.get(
    'POLITICIAN_ADS_DB',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'politician_ads_public.db'),
)

VIOLATION_TEXT   = 'violation of tiktok'       # substring match, case-insensitive
REMOVED_TEXT     = 'removed from tiktok'
INACTIVE_TEXT    = 'this ad is no longer'

ENFORCEMENT_STMT = "Removed from TikTok due to a violation of TikTok's terms"
VOLUNTARY_STMT   = "Campaign ended — advertiser stopped voluntarily"

DELAY_SECONDS = 1.5   # polite rate-limit between page loads


def classify_page(content: str) -> str:
    """Return 'enforcement', 'voluntary', or 'unknown'.

    NOTE: we intentionally do NOT classify as 'active' based on the word
    'active' appearing in the page — TikTok's library UI includes the word
    'active' in navigation/filter elements even for inactive/ended ads,
    causing false positives. An ad that disappeared from /ad/query/ is by
    definition no longer running; the only meaningful split is enforcement
    (violation message) vs voluntary stop (everything else).
    """
    low = content.lower()
    if VIOLATION_TEXT in low or REMOVED_TEXT in low:
        return 'enforcement'
    # Page rendered without violation message → campaign ended voluntarily
    # (includes budget exhausted, advertiser paused, or natural campaign end)
    if len(content) > 5000:   # page actually loaded (not empty/error/CAPTCHA)
        return 'voluntary'
    return 'unknown'


def check_ads(ad_ids: list[tuple], dry_run: bool) -> tuple[int, int, int, int]:
    """Visit each disappeared ad's library URL and classify it.

    Collects all results from the headless browser first, then writes all
    DB updates in a single db_lock session. This keeps the file lock held
    for milliseconds (batch UPDATE) rather than minutes (entire browser session).

    Returns (n_enforcement, n_voluntary, n_unknown, n_active).
    """
    from playwright.sync_api import sync_playwright

    n_enforcement = n_voluntary = n_unknown = n_active = 0
    # Collect results before touching the DB — lock held only during the write.
    results: list[tuple[str, str, str]] = []   # (ad_id, name, result)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
            locale='en-US',
        )
        page = ctx.new_page()

        for i, (ad_id, name) in enumerate(ad_ids, 1):
            url = f'https://library.tiktok.com/ads/detail/?ad_id={ad_id}'
            print(f'  [{i}/{len(ad_ids)}] @{name} — ad {ad_id}', end=' ', flush=True)

            try:
                page.goto(url, wait_until='networkidle', timeout=20_000)
                content = page.content()
                result  = classify_page(content)
            except Exception as e:
                print(f'ERROR: {e}')
                result = 'unknown'

            print(f'→ {result}')
            results.append((ad_id, name, result))

            if result == 'enforcement':   n_enforcement += 1
            elif result == 'voluntary':   n_voluntary   += 1
            elif result == 'active':      n_active      += 1
            else:                         n_unknown     += 1

            time.sleep(DELAY_SECONDS)

        browser.close()

    if dry_run or not results:
        return n_enforcement, n_voluntary, n_unknown, n_active

    # Batch-write all classifications under one lock so concurrent refreshes
    # don't race on the same ad rows. DB connection is opened INSIDE the lock
    # to guarantee serialisation. The try/finally ensures the connection is
    # always closed even if the UPDATE fails.
    now = datetime.now(timezone.utc).isoformat()
    with db_lock(DB):
        conn = sqlite3.connect(DB)
        try:
            for ad_id, name, result in results:
                if result == 'enforcement':
                    conn.execute(
                        "UPDATE tiktok_ads SET status_statement=?, last_status_check=? WHERE ad_id=?",
                        (ENFORCEMENT_STMT, now, ad_id),
                    )
                elif result == 'voluntary':
                    conn.execute(
                        "UPDATE tiktok_ads SET status_statement=?, last_status_check=? WHERE ad_id=?",
                        (VOLUNTARY_STMT, now, ad_id),
                    )
            conn.commit()
        finally:
            conn.close()

    return n_enforcement, n_voluntary, n_unknown, n_active


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--limit',   type=int, default=None)
    args = p.parse_args()

    started_at = datetime.now(timezone.utc).isoformat()
    crash_msg  = None

    conn = sqlite3.connect(DB)
    rows = conn.execute("""
        SELECT ad_id, advertiser_disclosed_name
        FROM tiktok_ads
        WHERE status_statement = 'No longer returned by API — deactivated or removed'
        ORDER BY advertiser_disclosed_name, last_shown DESC
    """).fetchall()
    conn.close()

    if args.limit:
        rows = rows[:args.limit]

    print(f'  {len(rows)} ads to classify via headless browser\n')
    if not rows:
        print('  Nothing to do.')
        if not args.dry_run:
            _ph.record(DB, run_kind='headless_classify', started_at=started_at,
                       status='ok', ads_checked=0, changes=0)
        return

    n_enforcement = n_voluntary = n_unknown = n_active = 0
    try:
        n_enforcement, n_voluntary, n_unknown, n_active = \
            check_ads(rows, dry_run=args.dry_run)
    except Exception as e:
        crash_msg = repr(e)
        raise
    finally:
        n_classified = n_enforcement + n_voluntary
        print(f'\n  ✓ enforcement={n_enforcement}  voluntary={n_voluntary}  '
              f'active={n_active}  unknown={n_unknown}')
        if not args.dry_run:
            _ph.record(
                DB,
                run_kind='headless_classify',
                started_at=started_at,
                status='failed' if crash_msg else 'ok',
                ads_checked=len(rows),
                changes=n_classified,
                error_msg=crash_msg,
            )


if __name__ == '__main__':
    main()
