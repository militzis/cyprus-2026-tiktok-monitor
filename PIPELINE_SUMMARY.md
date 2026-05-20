# Cyprus 2026 TikTok Ad Monitor — Full Pipeline Summary

*Generated 2026-05-20. Last updated 2026-05-20 (Incidents 5 + 6 added). Refresh this file if the architecture changes significantly.*

---

## What It Does

Monitors political advertising on TikTok during Cyprus's 2026 parliamentary elections
(election day: **Sunday 2026-05-24**). Detects violations (TikTok globally bans paid
political ads), takedowns, new candidates, and spend trends. Data lands on a Streamlit
dashboard via GitHub (auto-redeploys on every commit).

---

## Repositories & Key Locations

| Location | Role |
|---|---|
| `C:\Users\milit\dev\cyprus-2026-tiktok-monitor\` | Deploy repo (runs on GitHub Actions) |
| `politician_ads_public.db` | SQLite DB (committed to repo, read by Streamlit) |
| `C:\Users\milit\OneDrive\Documents\META library content\` | Local working dir (local scripts, CSV exports) |

---

## Three Cron Cadences

### 1. Daily — 04:00 UTC (`daily_status_refresh.yml`)

1. `refresh_ad_statuses.py --since 24h --limit 500` — updates status on existing ads
2. `refresh_known_catalogs.py` — re-fetches CY ad catalog for ~74 known political-tier advertisers → picks up new ads they posted
3. `tiktok_tier2_fetch.py --limit 300 --since-days 30` — targeting/demographics enrichment
4. `compute_spend_estimates.py --only-null` — EUR spend estimates
5. `auto_review.py --bucket recent_promotions --limit 30` — Claude Haiku second-opinion
6. `status_change_report.py` → `reports/YYYY-MM-DD.md`
7. `export_tiktok_daily_csv.py` → `reports/daily/tiktok_changes_*.csv` + `tiktok_summary_*.csv`
8. `strip_public_db.py` — removes unverified rows before commit
9. Commit + push

### 2. Weekly — Sunday 02:00 UTC (`weekly_discovery.yml`)

1. `discover_tiktok_ads.py` — name search over every candidate + party
2. `discover_content_keywords.py` — sweeps ~190 political keywords (finds supporters, agencies)
3. Enrichment chain (tier2 → spend → auto-review)
4. `strip_public_db.py`
5. Commit + push

### 3. Election-week — Every 3h (`election_week.yml`) — **active now**

1. `refresh_ad_statuses.py --since 3h --limit 200`
2. `discover_content_keywords.py` (lightweight discovery)
3. `refresh_known_catalogs.py`
4. `tiktok_tier2_fetch.py --limit 75`
5. Spend + auto-review (small limits: 10+5)
6. `strip_public_db.py`
7. Commit + push
8. **Election-silence violation check** (2026-05-22 15:00 UTC → 2026-05-24 18:00 UTC)

---

## Three Discovery Mechanisms (and what each misses)

| Script | Finds | Misses |
|---|---|---|
| `refresh_ad_statuses.py` | Status updates on EXISTING ads | Never adds new rows |
| `discover_content_keywords.py` | New advertisers via keyword sweep | **Explicitly skips known advertiser IDs** |
| `discover_tiktok_ads.py` | New advertisers by candidate name | Only runs weekly |
| `refresh_known_catalogs.py` | **New ads from known advertisers** (gap-filler) | Nothing — this IS the gap-filler |

---

## Classification Tiers (`match_type` column in `tiktok_ads`)

**Public** (dashboard-visible):

`manual_resume`, `party_account`, `party_coordinator`, `party_supporter`,
`political_movement`, `commentator`, `news_outlet`, `podcast`, `satirist`,
`politician_non_candidate`

**Stripped** (never shown on dashboard, removed by `strip_public_db.py`):

- `content_keyword` — raw keyword hits, unverified
- False positives (`is_fp = 1`)
- Numeric-handle rows (TikTok API quirk where `business_name` = numeric ID)

---

## Known Problems & Incidents (all 2026-05-20)

### 🔴 Incident 1 — 212 rows lost to numeric-handle strip *(fixed `c58f264`)*

**What happened:** TikTok's `/ad/query/` returned numeric `business_name` for ~15
known candidates during a re-fetch. The fallback in `_build_row()` was
`str(advertiser_id)` (also numeric). When `strip_public_db.py` ran, it deleted all
rows with numeric handles → 212 ads vanished from the dashboard.

**Fix:** `refresh_known_catalogs.py` now uses the **existing readable handle** from the
DB as fallback (`existing_handle` field in the classification dict). The 212 rows were
manually restored.

**Status:** ✅ Fixed and deployed.

---

### 🔴 Incident 2 — 483 unverified `content_keyword` rows leaked to dashboard *(fixed `56cc89a`)*

**What happened:** `discover_content_keywords.py` writes `content_keyword` rows into
`politician_ads_public.db`, but `strip_public_db.py` wasn't wired into daily or
election-week crons — only weekly.

**Fix:** `strip_public_db.py` step added to **all three** workflow files (daily +
election-week + weekly).

**Status:** ✅ Fixed and deployed. The 483 leaked rows were stripped manually via
`d80d866`.

---

### 🔴 Incident 3 — 49 takedowns counted as "stopped running" *(fixed `749933a`)*

**What happened:** TikTok sets `status = 'inactive'` (not `removed_by_tiktok`) during
enforcement and only reveals the takedown in `status_statement`. The first daily CSV
report counted 49 transition events as voluntary stops instead of enforcement actions.

**Fix:** `_effective_label()` in both copies of `export_tiktok_daily_csv.py` now checks
for `'removed'`/`'violation'` in the statement and upgrades the label to
`REMOVED BY TIKTOK`.

**Status:** ✅ Fixed and deployed. There are **two copies** of this script:
- Deploy repo: `C:\Users\milit\dev\cyprus-2026-tiktok-monitor\export_tiktok_daily_csv.py`
- Local/OneDrive: `C:\Users\milit\OneDrive\Documents\META library content\export_tiktok_daily_csv.py`

Both have the fix. `sync_shared.py` manages drift between the two.

---

### 🟡 Incident 4 — Election-week cron timed out (2026-05-19 22:01 UTC)

**What happened:** `--limit 400` on status refresh burned the full 45-minute wall.
TikTok was running ~6.7s/ad (rate-limit backoffs rather than the expected 2.5s).
All downstream steps were skipped; nothing committed.

**Fix:** Limits lowered to `--limit 200` (refresh) and `--limit 75` (tier2). Wall time
raised from 45 → **60 minutes**.

**Status:** ✅ Fixed. API speed under election-week load remains unpredictable — if
timeouts recur, lower limits further or split into two jobs.

---

### 🔴 Incident 5 — `tiktok_tier2_fetch.py` 100% failure: HTTP 400 on all enrich calls *(fixed `b955277`)*

**What happened:** TikTok silently changed `/v2/research/adlib/ad/detail/` to use
dot-notation grouped field names (e.g. `ad_group.targeting_info`, `ad.reach`,
`advertiser.business_name`) instead of the old flat names (`age`, `gender`, `country`,
`follower_count`, etc.). Every call to the enrich endpoint returned HTTP 400
`invalid_params`, listing all the old flat names as invalid. 300 failed calls/day
burned the daily `/ad/detail/` quota while writing no targeting data.

**Fix:** Updated `FIELDS` in `tiktok_tier2_fetch.py` to the current dot-notation field
list. Updated `enrich_one()` to unpack the nested response objects
(`data.ad.reach`, `data.ad_group.targeting_info`, `data.advertiser.*`).

**Status:** ✅ Fixed and deployed (`b955277`).

---

### 🔴 Incident 6 — `refresh_ad_statuses.py` exhausted `/ad/detail/` daily quota *(fixed `0117a5b`)*

**What happened:** `refresh_ad_statuses.py` called `/ad/detail/` once per ad:
200 ads × 8 election-week ticks/day + 500 ads × 1 daily run ≈ 2,100 calls/day.
The `/ad/detail/` endpoint has a ~500 call/day quota. By midday every day the
quota was exhausted. Every afternoon election-week run hit persistent 429 errors
from the very first call. Each failed ad burned 30+60+120s of exponential backoff
before moving to the next ad (which also hit 429 immediately). The 60-minute wall
was burned by backoffs alone — zero status updates were written, nothing committed.

Dashboard symptoms: "Total ads" KPI unchanged for 2 days; all election-week runs
showing `candidates to refresh: 200` then immediate 429s.

**Fix:** Rewrote `refresh_ad_statuses.py` to use `/ad/query/` (per-advertiser bulk
endpoint) instead of `/ad/detail/` (per-ad low-quota endpoint):
- ~74 known advertisers × ~2 pages = ~150 `/ad/query/` calls per run
- `/ad/query/` is the bulk discovery endpoint with a much higher rate limit
- `/ad/detail/` is now reserved exclusively for `tiktok_tier2_fetch.py`
- `_refresh_loop()` completely rewritten; `fetch_ad_detail()` removed
- `_adv_ids_to_refresh()` selects distinct `advertiser_id` values whose
  `last_status_check` is older than the `--since` window

**Status:** ✅ Fixed and deployed (`0117a5b`).

---

### 🟡 Deferred TODO #6 — Duplicate `pipeline_health` schema

`_ensure_health_schema()` is copy-pasted in both `refresh_ad_statuses.py` and
`refresh_known_catalogs.py`. Comment in code says "extract to shared helper." Non-blocking
but creates maintenance risk if the schema ever needs a new column.

---

## Election Countdown

| Event | Date/Time |
|---|---|
| Election-silence window starts | **2026-05-22 15:00 UTC** (18:00 Cyprus) |
| Election day | **Sunday 2026-05-24** |
| Silence window ends (polls close) | **2026-05-24 18:00 UTC** |

Election-week cron (`0 */3 * * *`) is live. The silence-violation check runs at every
3h tick and emits a `::error` GitHub Actions annotation if any political ad is still
`active` during the blackout window.

---

## Local ↔ Deploy Sync

`sync_shared.py` in the main (local) repo defines a `MANIFEST` of 12 files with
`canonical='deploy'` — these live in the deploy repo and must be synced to local if
edited there. Run `python sync_shared.py` locally to check drift.

`tests/test_shared_files_sync.py` in the deploy repo verifies all 12 files exist and
import cleanly. This test runs at the top of every cron (daily + weekly) before any
API calls touch the live DB.

---

## Key API Notes

- **OAuth:** Client-credentials flow; 2-hour token cached in `tiktok_token_cache.json`
- **Rate limits:** No documented quota; empirically: 30/60/120/240s exponential backoff + circuit breaker after 3 consecutive 429s (10 min sleep). Daily quota window resets overnight.
- **Numeric business_name quirk:** TikTok occasionally returns the numeric `advertiser_id` as `business_name` on re-fetches. `tiktok_api.resolve_disclosed_name()` + `is_numeric_handle_quirk()` handle this. Always use existing handle as fallback in `refresh_known_catalogs.py`.
- **Takedown disclosure quirk:** Status = `inactive` + statement contains `'removed'`/`'violation'` = real takedown. Do NOT rely on `removed_by_tiktok` status code alone.
