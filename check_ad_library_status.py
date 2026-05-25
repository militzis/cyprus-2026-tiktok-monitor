"""check_ad_library_status.py — Use a headless browser to check the TikTok Ad
Library page for each 'disappeared' ad and determine if it was:

  - enforcement:  "removed from TikTok due to a violation of TikTok's terms"
  - voluntary:    "Campaign ended — advertiser stopped voluntarily"
  - active:       ad is still showing as active on the library
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
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None

DB = r'C:\Users\milit\dev\cyprus-2026-tiktok-monitor\politician_ads_public.db'

VIOLATION_TEXT  = 'violation of tiktok'       # substring match, case-insensitive
REMOVED_TEXT    = 'removed from tiktok'
INACTIVE_TEXT   = 'this ad is no longer'

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
    if len(content) > 5000:   # page actually loaded (not empty/error)
        return 'voluntary'
    return 'unknown'


def check_ads(ad_ids: list[tuple], dry_run: bool) -> None:
    from playwright.sync_api import sync_playwright

    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB)

    n_enforcement = n_voluntary = n_unknown = n_active = 0

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

            if not dry_run:
                if result == 'enforcement':
                    conn.execute(
                        "UPDATE tiktok_ads SET status_statement=?, last_status_check=? WHERE ad_id=?",
                        (ENFORCEMENT_STMT, now, ad_id)
                    )
                    conn.commit()
                    n_enforcement += 1
                elif result == 'voluntary':
                    conn.execute(
                        "UPDATE tiktok_ads SET status_statement=?, last_status_check=? WHERE ad_id=?",
                        (VOLUNTARY_STMT, now, ad_id)
                    )
                    conn.commit()
                    n_voluntary += 1
                elif result == 'active':
                    n_active += 1
                else:
                    n_unknown += 1
            else:
                if result == 'enforcement': n_enforcement += 1
                elif result == 'voluntary': n_voluntary += 1
                elif result == 'active':    n_active += 1
                else:                       n_unknown += 1

            time.sleep(DELAY_SECONDS)

        browser.close()
    conn.close()

    print(f'\n  ✓ enforcement={n_enforcement}  voluntary={n_voluntary}  '
          f'active={n_active}  unknown={n_unknown}')


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--limit',   type=int, default=None)
    args = p.parse_args()

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
        return

    check_ads(rows, dry_run=args.dry_run)


if __name__ == '__main__':
    main()
