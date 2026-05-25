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
import pipeline_health as _ph

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

# Supplementary per-advertiser pass window (days).
# The batch API (/ad/query/ with many advertiser_ids) stops pagination
# early when results are dense, silently missing ads from heavy advertisers
# (observed 2026-05-25: 10 @adiafthoroi election-day ads absent from batch
# but present in per-advertiser call). After the batch we do a second pass
# querying each advertiser individually for the last N days — cheap because
# most advertisers return 0-1 pages in a short window, and it guarantees
# completeness for the most recent (and most important) ads.
PER_ADV_RECENT_DAYS = 10


def default_since() -> str:
    return (date.today() - timedelta(days=DEFAULT_SINCE_DAYS)).strftime('%Y-%m-%d')



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

    # Fix: upsert_rows uses discover_tiktok_ads.DB_PATH which defaults to the
    # master DB (meta_pipeline_data/politician_ads.db). Point it at the same
    # DB this script uses so new ads land in the right place.
    t.DB_PATH = DB

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
        print(f"  {n_advertisers} known advertisers (since {since})", flush=True)

        # Build classification map and filter out recently-checked advertisers.
        classification_map: dict[str, dict] = {}
        adv_ids_to_query: list[int] = []
        now_utc = datetime.now(timezone.utc)

        for (adv_id, mt, cand, party, district, existing_handle, last_check) in adv_rows:
            classification_map[str(adv_id)] = {
                'match_type':        mt,
                'matched_candidate': cand,
                'matched_party':     party,
                'matched_district':  district,
                'existing_handle':   existing_handle,
            }
            if skip_if_recent_hours and last_check:
                try:
                    age_h = (now_utc - datetime.fromisoformat(
                                 last_check.replace('Z', '+00:00'))
                             ).total_seconds() / 3600
                    if age_h < skip_if_recent_hours:
                        n_skipped += 1
                        continue
                except Exception:
                    pass  # malformed timestamp — query anyway
            adv_ids_to_query.append(int(adv_id))

        if not adv_ids_to_query:
            print(f"  all {n_skipped} advertisers checked within the last "
                  f"{skip_if_recent_hours}h — nothing to do.", flush=True)
            return 0

        print(f"  batch-querying {len(adv_ids_to_query)} advertisers "
              f"({n_skipped} skipped as recently checked)", flush=True)

        t.get_access_token()

        # ── BATCH QUERY ───────────────────────────────────────────────────────
        # Single call (paginated) for ALL advertisers instead of 67 individual
        # calls. Reduces ~134 API requests to ~5-20 paginated pages.
        # Falls back to per-advertiser if the batch call raises unexpectedly.
        try:
            all_ads = t.query_ads_batch(adv_ids_to_query, since)
        except t.DailyQuotaExceeded:
            print("  [429] daily_quota_limit_exceeded — quota resets at 00:00 UTC. "
                  "No point retrying today.", flush=True)
            return 0   # exit 0: not our fault, nothing to fix, don't alarm GH Actions
        except t.RateLimitExceeded:
            print("  [429] rate-limited on batch query — stopping.", flush=True)
            return 1

        print(f"  batch returned {len(all_ads)} ads total", flush=True)

        # Collect all ad_ids the API returned — used below for disappearance detection.
        api_returned_ids: set[str] = {
            str((item.get('ad', {}) or {}).get('id') or '')
            for item in all_ads
            if (item.get('ad', {}) or {}).get('id')
        }

        # Build reverse map: existing ad_id → our DB's advertiser_id.
        # Needed to correctly route batch results when TikTok returns a different
        # business_id in the response than the one we queried with (observed quirk:
        # e.g. we query 7631588801290272785, API returns business_id 7631588817719263254).
        # Without this, grouping by returned business_id silently skips upserts for
        # those advertisers. For existing ads the ad_id lookup is authoritative;
        # for new ads we fall back to the returned business_id (and the per-adv
        # supplementary pass will catch any that still slip through).
        _existing_ad_to_adv: dict[str, str] = {
            r[0]: str(r[1])
            for r in sqlite3.connect(DB).execute(
                'SELECT ad_id, advertiser_id FROM tiktok_ads')
        }

        # Group batch results by OUR advertiser_id (not the returned business_id).
        items_by_our_adv: dict[str, list] = {}
        for item in all_ads:
            av_obj = item.get('advertiser', {}) or {}
            ad_obj = item.get('ad', {}) or {}
            ad_id  = str(ad_obj.get('id') or '')
            bid    = str(av_obj.get('business_id') or '')
            # Existing ad → authoritative DB adv_id; new ad → returned bid (best-effort)
            our_id = _existing_ad_to_adv.get(ad_id) or (
                bid if bid in classification_map else None)
            if our_id:
                items_by_our_adv.setdefault(our_id, []).append(item)

        # Upsert each advertiser's ads and stamp last_status_check.
        stamp_ts = datetime.now(timezone.utc).isoformat()
        for adv_id_int in adv_ids_to_query:
            adv_id         = str(adv_id_int)
            classification = classification_map[adv_id]
            items          = items_by_our_adv.get(adv_id, [])

            if items:
                new_rows = [_build_row(item, classification, adv_id)
                            for item in items]
                for row in new_rows:
                    if row['ad_id'] and row['ad_id'] not in existing_ids:
                        n_new_ads += 1
                        existing_ids.add(row['ad_id'])
                t.upsert_rows(new_rows)

            # Stamp last_status_check regardless — even advertisers with no
            # new ads in the window are "checked" and should be skipped by
            # refresh_ad_statuses.py --since 24h on the next run.
            try:
                _conn = sqlite3.connect(DB)
                try:
                    _conn.execute(
                        "UPDATE tiktok_ads SET last_status_check = ?"
                        " WHERE advertiser_id = ?",
                        (stamp_ts, adv_id),
                    )
                    _conn.commit()
                finally:
                    _conn.close()
            except Exception as _e:
                print(f"  ⚠ last_status_check stamp failed for {adv_id}: {_e!r}",
                      flush=True)

        # ── SUPPLEMENTARY PER-ADVERTISER PASS (catch batch pagination gaps) ──
        # The batch API silently under-returns when many advertisers are queried
        # together — it stops paginating early, missing ads from heavy
        # advertisers (e.g. a candidate who burst 10 ads on election day).
        # This pass queries each advertiser individually for the last
        # PER_ADV_RECENT_DAYS days, where the gap matters most, and upserts
        # anything the batch missed. It also corrects stale last_shown values
        # on existing ads (batch may return an old last_shown while per-adv
        # returns the true current value). Cost: ~1-2 API calls per advertiser.
        _recent_since = max(
            since,
            (datetime.now(timezone.utc).date() -
             timedelta(days=PER_ADV_RECENT_DAYS)).strftime('%Y-%m-%d'),
        )
        print(f"\n  supplementary per-adv pass (since {_recent_since}, "
              f"{PER_ADV_RECENT_DAYS}d window)...", flush=True)
        n_supp_new = 0
        _supp_quota_hit = False
        for adv_id_int in adv_ids_to_query:
            adv_id         = str(adv_id_int)
            classification = classification_map[adv_id]
            try:
                items = t.query_ads_for_advertiser(adv_id_int, _recent_since)
            except t.DailyQuotaExceeded:
                print("  [429] daily quota — stopping supplementary pass.",
                      flush=True)
                _supp_quota_hit = True
                break
            except t.RateLimitExceeded:
                print("  [429] rate-limited — stopping supplementary pass.",
                      flush=True)
                _supp_quota_hit = True
                break
            if not items:
                continue
            _new_here   = 0
            rows_to_upsert = []
            for item in items:
                ad_obj = item.get('ad', {}) or {}
                ad_id  = str(ad_obj.get('id') or '')
                if not ad_id:
                    continue
                row = _build_row(item, classification, adv_id_int)
                rows_to_upsert.append(row)
                # Track truly new ids for counts and disappearance detection
                if ad_id not in existing_ids:
                    existing_ids.add(ad_id)
                    n_supp_new += 1
                    _new_here  += 1
                # Always add to api_returned_ids so disappearance detection
                # doesn't falsely flag ads the batch missed as "disappeared".
                api_returned_ids.add(ad_id)
            if rows_to_upsert:
                t.upsert_rows(rows_to_upsert)
            if _new_here:
                handle = classification.get('existing_handle', adv_id)
                print(f"    @{handle}: +{_new_here} new ads", flush=True)

        if not _supp_quota_hit:
            print(f"  supplementary pass done: {n_supp_new} new ads added",
                  flush=True)
        n_new_ads += n_supp_new

        # ── DISAPPEARANCE DETECTION (all political-tier advertisers) ─────────
        # After the batch query we know exactly which ads the API returned.
        # Any DB-active ad within the query window that was NOT returned →
        # it was deactivated (advertiser stopped it or TikTok removed it).
        # We mark it inactive so the DB doesn't accumulate stale "active"
        # records. We can't distinguish voluntary stop from TikTok enforcement
        # here — check the library URL manually to confirm enforcement.
        queried_adv_ids = [str(i) for i in adv_ids_to_query]
        n_deactivated = 0
        if queried_adv_ids:
            ph = ','.join('?' * len(queried_adv_ids))
            _conn = sqlite3.connect(DB)
            try:
                db_active = _conn.execute(f"""
                    SELECT ad_id, advertiser_disclosed_name, match_type
                    FROM tiktok_ads
                    WHERE ad_status = 'active'
                      AND last_shown >= ?
                      AND advertiser_id IN ({ph})
                """, [since] + queried_adv_ids).fetchall()
                # NOTE: we filter by last_shown >= since, NOT first_shown >= since.
                # Reason: the batch query covers ads published since `since`, so any
                # ad with last_shown >= since should reappear in the API response if
                # still running. An ad with last_shown < since is an old campaign that
                # ended before our window — we can't detect its disappearance without
                # a wider API query, and it's almost certainly already ended (skip it).
                # Using first_shown >= since was wrong: it missed 90 active ads whose
                # campaigns started before the window but were still shown recently.

                for ad_id, name, mt in db_active:
                    if ad_id not in api_returned_ids:
                        _conn.execute("""
                            UPDATE tiktok_ads
                            SET ad_status        = 'inactive',
                                status_statement = 'No longer returned by API — deactivated or removed',
                                last_status_check = ?
                            WHERE ad_id = ?
                        """, (stamp_ts, ad_id))
                        n_deactivated += 1
                        print(f"  ⚠ disappeared [{mt}]: @{name} ad {ad_id} → inactive",
                              flush=True)
                if n_deactivated:
                    _conn.commit()
                    print(f"  → {n_deactivated} ads marked inactive "
                          f"(disappeared from API)", flush=True)
            finally:
                _conn.close()

        print(f"\n  ✓ batch-refreshed {len(adv_ids_to_query)} advertisers "
              f"({n_skipped} skipped), {n_new_ads} new ads, "
              f"{n_deactivated} deactivated",
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
        _ph.record(
            DB,
            run_kind='catalog_refresh',
            started_at=started_at,
            status='failed' if crash_msg else 'ok',
            ads_checked=n_advertisers,
            changes=n_new_ads,
            since_arg=since,
            error_msg=msg,
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
