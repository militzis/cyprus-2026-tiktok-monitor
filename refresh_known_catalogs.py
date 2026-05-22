"""refresh_known_catalogs.py — re-fetch the CY ad lists of every advertiser
already in a verified political tier, so new ads they post show up on the
dashboard within hours instead of waiting for next Sunday's weekly run.

Why this script exists
----------------------
The pipeline has three discovery mechanisms with different jobs:

  refresh_ad_statuses.py     — UPDATES status on existing ad rows; never
                               adds new ads. (Cron: every 3h election week.)
  discover_content_keywords  — finds NEW advertisers via keyword sweep;
                               EXPLICITLY SKIPS known advertisers. (Cron:
                               election-week + weekly.)
  discover_tiktok_ads.py     — re-fetches every known advertiser's CY ad
                               catalog via query_ads_for_advertiser, so
                               new ads from existing candidates DO get
                               picked up. (Cron: weekly only.)

The trap: between Sunday weekly runs, when @fotinitsiridou posts a new
ad, NO cron catches it. Refresh updates her existing 49 ads' status and
ignores the new one. Keyword discovery sees her ad, finds her bid in
KNOWN_BIDS, and skips. Name-based discovery only runs Sunday.

This script fills that gap: it iterates the ~74 known political-tier
advertisers and re-queries each one's CY ad catalog, upserting any new
ad_ids while PRESERVING the advertiser's existing classification. Plugs
into election-week (every 3h) so dashboard freshness for candidates'
new ads drops from ~7 days to ~3 hours.

Why it preserves match_type
---------------------------
discover_tiktok_ads.upsert_rows uses ON CONFLICT(ad_id) DO UPDATE SET …
which OVERWRITES every column from `excluded`. If we naively built rows
with match_type='content_keyword' for new ads from an existing
manual_resume advertiser, those new rows would land as content_keyword
AND the upsert would not change the advertiser's already-promoted ads
(those are protected by their existing PK). But new rows would still
appear as content_keyword on the dashboard until manually promoted.

Instead we look up each advertiser's existing classification first and
construct new ad rows with the SAME match_type / matched_candidate /
matched_party / matched_district. So a new ad from @theodosisavgousti
lands as manual_resume from row 1.

Usage
-----
  python refresh_known_catalogs.py                  # since 2026-04-01
  python refresh_known_catalogs.py --since 2026-03-01
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discover_tiktok_ads as t
from tiktok_api import resolve_disclosed_name, resolve_funded_by

DB = os.environ.get('POLITICIAN_ADS_DB',
                    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 'politician_ads_public.db'))

# Same set as build_public_db keeps. Excludes content_keyword / FP /
# unverified — we only refresh catalogs for advertisers a human (or
# auto_review) has classified.
POLITICAL_TIERS = (
    'manual_resume', 'party_account', 'party_coordinator',
    'party_supporter', 'political_movement', 'commentator',
    'news_outlet', 'podcast', 'satirist', 'politician_non_candidate',
)

# Date floor for the ad query — rolling window. We only need to catch
# NEW ads the existing 74 advertisers posted since the last cron tick;
# anything older is already in the DB and re-fetching wastes pagination
# (a candidate with 89 ads → 2 pages per request → 8s instead of 4s).
# 60 days picks up the active campaign window without hardcoding a date
# that would silently go stale after the election. Override with --since
# YYYY-MM-DD if you ever need a deeper sweep (e.g. one-time backfill).
DEFAULT_SINCE_DAYS = 60


def default_since() -> str:
    return (date.today() - timedelta(days=DEFAULT_SINCE_DAYS)).strftime('%Y-%m-%d')


def _ensure_health_schema(conn: sqlite3.Connection) -> None:
    """Mirror of the schema in refresh_ad_statuses.py — kept inline rather
    than imported to avoid coupling this script to the refresh module's
    internal helpers. A follow-up refactor will extract pipeline_health
    into a shared helper (deferred TODO #6 from the architecture review)."""
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


def _record_health(run_kind: str, started_at: str, status: str,
                   ads_checked: int = 0, changes: int = 0, errors: int = 0,
                   error_msg: str | None = None, since_arg: str | None = None) -> None:
    """Best-effort heartbeat write. Never raises so a heartbeat failure
    can't take down the catalog-refresh run."""
    try:
        conn = sqlite3.connect(DB)
        _ensure_health_schema(conn)
        conn.execute("""
            INSERT INTO pipeline_health
              (run_kind, started_at, finished_at, status,
               ads_checked, changes, errors, error_msg, since_arg)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (run_kind, started_at, datetime.now(timezone.utc).isoformat(),
              status, ads_checked, changes, errors, error_msg, since_arg))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  ⚠ heartbeat write failed: {e!r}", flush=True)


def _build_row(item: dict, classification: dict, advertiser_id: str) -> dict:
    """Convert a /ad/query/ result item to an upsert row, PRESERVING the
    advertiser's existing political-tier classification (the critical bit
    that keeps new ads from being downgraded to content_keyword)."""
    ad_obj    = item.get('ad', {}) or {}
    av_obj    = item.get('advertiser', {}) or {}
    reach_raw = (ad_obj.get('reach') or {}).get('unique_users_seen') or ''
    lb, ub    = t.parse_reach(reach_raw)
    ad_id     = str(ad_obj.get('id') or '')
    # CRITICAL: fallback is the EXISTING readable handle (e.g.,
    # 'theodosisavgousti'), NOT str(advertiser_id). 2026-05-20 incident:
    # TikTok returned numeric business_name on a re-fetch for ~15 known
    # candidates, our fallback was the numeric advertiser_id, and the
    # strip step then deleted 212 rows because their disclosed_name
    # became numeric. Using the existing handle as fallback ensures the
    # row keeps its readable name even when /ad/query/ regresses.
    existing_handle = classification.get('existing_handle') or ''
    fallback        = existing_handle if existing_handle and not existing_handle.isdigit() else str(advertiser_id)
    disclosed = resolve_disclosed_name(av_obj, fallback=fallback)
    funded_by = resolve_funded_by(av_obj)
    return {
        'ad_id':                     ad_id,
        'advertiser_id':             str(advertiser_id),
        'advertiser_disclosed_name': disclosed,
        'ad_funded_by':              funded_by,
        'country_code':              'CY',
        'ad_url':                    f'https://library.tiktok.com/ads/detail/?ad_id={ad_id}' if ad_id else None,
        'first_shown':               t._fmt_date(ad_obj.get('first_shown_date', '')),
        'last_shown':                t._fmt_date(ad_obj.get('last_shown_date', '')),
        'ad_status':                 ad_obj.get('status'),
        'status_statement':          ad_obj.get('status_statement'),
        'videos_json':               json.dumps(ad_obj.get('videos') or [], ensure_ascii=False),
        'image_urls_json':           json.dumps(ad_obj.get('image_urls') or [], ensure_ascii=False),
        'reach_raw':                 reach_raw,
        'times_shown_lower_bound':   lb,
        'times_shown_upper_bound':   ub,
        'targeting_json':            None,
        # PRESERVED classification — this is what differentiates this
        # script from content_keyword discovery's auto-content_keyword tag.
        'matched_candidate':         classification['matched_candidate'],
        'matched_party':             classification['matched_party'],
        'matched_district':          classification['matched_district'],
        'match_type':                classification['match_type'],
        'is_political':              1,
    }


def main(since: str, skip_if_recent_hours: float | None = None) -> int:
    if not t.CLIENT_KEY or not t.CLIENT_SECRET:
        sys.exit("ERROR: TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET missing from env")

    started_at        = datetime.now(timezone.utc).isoformat()
    crash_msg         = None
    n_new_ads         = 0
    n_advertisers     = 0
    n_skipped         = 0
    t.reset_api_metrics()

    try:
        conn = sqlite3.connect(DB)
        # One classification per advertiser_id. GROUP BY collapses cases
        # where an advertiser has multiple rows (e.g., during a partial
        # re-tier transition); we accept any one of the political-tier
        # rows since promote.py keeps them all consistent.
        placeholders = ','.join('?' * len(POLITICAL_TIERS))
        # advertiser_disclosed_name added 2026-05-20: used as the fallback
        # for resolve_disclosed_name so a numeric-quirk re-fetch doesn't
        # replace a readable handle with the numeric business_id (which
        # the strip step would then delete).
        adv_rows = conn.execute(f"""
            SELECT advertiser_id, match_type, matched_candidate,
                   matched_party, matched_district,
                   advertiser_disclosed_name AS existing_handle,
                   MAX(last_status_check) AS last_check
            FROM tiktok_ads
            WHERE match_type IN ({placeholders})
            GROUP BY advertiser_id
        """, POLITICAL_TIERS).fetchall()
        existing_ids = {r[0] for r in conn.execute('SELECT ad_id FROM tiktok_ads')}
        conn.close()

        n_advertisers = len(adv_rows)
        print(f"  refreshing catalogs for {n_advertisers} known advertisers "
              f"(since {since})", flush=True)
        if skip_if_recent_hours:
            print(f"  skipping advertisers checked in the last "
                  f"{skip_if_recent_hours:.1f}h", flush=True)

        # Early-abort sentinel: if the first EARLY_ABORT_THRESHOLD advertisers
        # are all rate-limited, the API is saturated — stop immediately instead
        # of burning through all 67 advertisers with backoff delays.
        EARLY_ABORT_THRESHOLD = 5
        consecutive_429s_at_start = 0

        t.get_access_token()
        now_utc = datetime.now(timezone.utc)
        for i, (adv_id, mt, cand, party, district, existing_handle, last_check) in enumerate(adv_rows, 1):
            # Skip recently-checked advertisers to avoid hammering the API on
            # back-to-back election-week ticks (every 3h). The caller passes
            # skip_if_recent_hours=2.5 so we only re-query advertisers not
            # seen in the last 2.5h — practically a no-op if the previous
            # tick ran successfully.
            if skip_if_recent_hours and last_check:
                try:
                    age_h = (now_utc - datetime.fromisoformat(last_check.replace('Z', '+00:00'))
                             ).total_seconds() / 3600
                    if age_h < skip_if_recent_hours:
                        n_skipped += 1
                        continue
                except Exception:
                    pass  # malformed timestamp — proceed normally

            classification = {
                'match_type':        mt,
                'matched_candidate': cand,
                'matched_party':     party,
                'matched_district':  district,
                'existing_handle':   existing_handle,
            }
            try:
                ads = t.query_ads_for_advertiser(int(adv_id), since)
                consecutive_429s_at_start = 0  # reset on any success
            except t.RateLimitExceeded:
                if i <= EARLY_ABORT_THRESHOLD:
                    consecutive_429s_at_start += 1
                    if consecutive_429s_at_start >= EARLY_ABORT_THRESHOLD:
                        print(f"  [early-abort] first {EARLY_ABORT_THRESHOLD} advertisers "
                              f"all rate-limited — API saturated, stopping run.",
                              flush=True)
                        break
                print(f"  [429] persistent — stopping at {i}/{n_advertisers}",
                      flush=True)
                break
            except Exception as e:
                print(f"  [ERR] advertiser_id={adv_id}: {type(e).__name__}: {e}",
                      flush=True)
                continue

            if not ads:
                continue
            new_rows = [_build_row(item, classification, adv_id) for item in ads]
            # Count truly-new ad_ids (existing ones are still upserted to
            # pick up any status/reach changes — that's the secondary
            # benefit of this script).
            for row in new_rows:
                if row['ad_id'] and row['ad_id'] not in existing_ids:
                    n_new_ads += 1
                    existing_ids.add(row['ad_id'])
            t.upsert_rows(new_rows)
            # Stamp last_status_check so refresh_ad_statuses.py --since Xh
            # skips these advertisers when it runs next (daily cron). Without
            # this, both scripts query /ad/query/ for the same 74 advertisers
            # in the same pipeline run, doubling the API load and causing 429s.
            # Added 2026-05-20 after catalog refresh + status refresh were
            # both switched to /ad/query/ and started exhausting the quota.
            try:
                _conn = sqlite3.connect(DB)
                try:
                    _conn.execute(
                        "UPDATE tiktok_ads SET last_status_check = ? WHERE advertiser_id = ?",
                        (datetime.now(timezone.utc).isoformat(), str(adv_id)),
                    )
                    _conn.commit()
                finally:
                    _conn.close()
            except Exception as _e:
                print(f"  ⚠ last_status_check stamp failed for {adv_id}: {_e!r}", flush=True)

            if i % 10 == 0:
                print(f"  ... {i}/{n_advertisers} processed "
                      f"(new ads so far: {n_new_ads})", flush=True)

        print(f"\n  ✓ refreshed {n_advertisers} catalogs "
              f"({n_skipped} skipped as recent), {n_new_ads} new ads",
              flush=True)
        return 0

    except SystemExit:
        raise
    except Exception as e:
        crash_msg = repr(e)
        raise
    finally:
        api_summary = t.print_api_summary('catalog_refresh')
        msg = crash_msg if crash_msg else (api_summary or None)
        _record_health(
            run_kind='catalog_refresh',
            started_at=started_at,
            status='failed' if crash_msg else 'ok',
            ads_checked=n_advertisers,
            changes=n_new_ads,
            errors=0,
            error_msg=msg,
            since_arg=since,
        )


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--since', default=None,
                   help=f'Date floor (YYYY-MM-DD) for the ad query. '
                        f'Default: today - {DEFAULT_SINCE_DAYS} days '
                        f'(auto-adapts so the cutoff stays a rolling '
                        f'window — no hardcoded date to go stale).')
    p.add_argument('--skip-if-recent-hours', type=float, default=None,
                   metavar='N',
                   help='Skip advertisers whose last_status_check is less than '
                        'N hours ago. Use in election-week (every 3h tick) to '
                        'avoid re-querying advertisers checked in the previous '
                        'tick. Recommended: 2.5 (covers 3h interval with margin).')
    args = p.parse_args()
    sys.exit(main(args.since or default_since(),
                  skip_if_recent_hours=args.skip_if_recent_hours))
