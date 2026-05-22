"""Refresh ad statuses via /ad/query/ (per-advertiser bulk) — NOT /ad/detail/.

Strategy (updated 2026-05-20):
  For each distinct advertiser_id in the DB, call query_ads_for_advertiser()
  which pages through /v2/research/adlib/ad/query/ and returns all their CY ads
  with the current status + status_statement in one round-trip per ~50 ads.

  OLD approach (broken): called /ad/detail/ once per ad — 200 ads × 8 election-week
  ticks/day = 1,600 daily calls, PLUS 312 from the daily run = ~2,000 calls/day.
  The /ad/detail/ endpoint has a low daily quota (~500 calls) and was being exhausted
  by lunchtime, causing every afternoon election-week run to hit persistent 429s and
  burn through the 60-minute wall on backoffs.

  NEW approach: ~74 known advertisers × ~2 pages each = ~150 /ad/query/ calls/day.
  /ad/query/ has a much higher rate limit (it is the bulk discovery endpoint).
  /ad/detail/ is now reserved for the enrich step only (demographics, targeting).

Status values seen so far from TikTok's API:
  - "active"               — ad is currently being shown
  - "inactive"             — advertiser stopped or budget exhausted
  - "removed_by_tiktok"    — TikTok removed it (policy violation)
  - "deleted_by_advertiser"
  - "expired"

Usage:
  python refresh_ad_statuses.py                  # refresh advertisers with stale checks
  python refresh_ad_statuses.py --all            # force-refresh all advertisers
  python refresh_ad_statuses.py --since 3h       # stale = not checked in last 3h
  python refresh_ad_statuses.py --limit 50       # cap to first N advertisers
"""
import os, sys, time, sqlite3, argparse, json, subprocess
from datetime import datetime, timedelta, timezone
sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discover_tiktok_ads as t

DB = os.environ.get('POLITICIAN_ADS_DB',
                    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 'politician_ads_public.db'))
# How often to git-commit + push the in-progress DB inside CI. With the old
# per-ad approach, 50 ads ≈ 5 minutes. Now per-advertiser, 10 advertisers ≈
# 5-10 API calls (still safe checkpoint cadence without over-committing).
CHECKPOINT_EVERY = 10

# How far back to query each advertiser's ad catalog. Wide enough to catch
# all active election-campaign ads without re-fetching the full 2+ year history.
REFRESH_SINCE_DAYS = 90


def ensure_schema(conn: sqlite3.Connection):
    """Add the status-changes log table + last_status_check column + the
    pipeline_health heartbeat table."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tiktok_ad_status_changes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id           TEXT    NOT NULL,
            observed_at     TEXT    NOT NULL,
            prev_status     TEXT,
            new_status      TEXT,
            prev_statement  TEXT,
            new_statement   TEXT,
            advertiser_id   TEXT,
            handle          TEXT
        );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status_changes_ad_id ON tiktok_ad_status_changes(ad_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status_changes_observed ON tiktok_ad_status_changes(observed_at);")

    # Heartbeat table — every refresh writes one row at the end. Dashboard
    # reads MAX(finished_at) to show "last refresh: N hours ago"; if older
    # than 25h it surfaces a red warning. Without this, a silently-failed
    # cron is invisible until someone notices the data is stale (today's
    # bug class).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_health (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_kind     TEXT    NOT NULL,    -- 'refresh' | 'discover' | etc.
            started_at   TEXT    NOT NULL,
            finished_at  TEXT    NOT NULL,
            status       TEXT    NOT NULL,    -- 'ok' | 'failed'
            ads_checked  INTEGER,
            changes      INTEGER,
            errors       INTEGER,
            error_msg    TEXT,                -- non-null when status='failed'
            since_arg    TEXT,                -- CLI args, for debugging
            limit_arg    INTEGER
        );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_health_finished "
                 "ON pipeline_health(finished_at);")

    # Add last_status_check column if missing
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tiktok_ads)").fetchall()]
    if 'last_status_check' not in cols:
        conn.execute("ALTER TABLE tiktok_ads ADD COLUMN last_status_check TEXT;")
    conn.commit()


def record_health(conn, run_kind, started_at, status,
                  ads_checked=None, changes=None, errors=None,
                  error_msg=None, since_arg=None, limit_arg=None):
    """Insert one row into pipeline_health. Always called from a top-level
    try/finally so even crashes get recorded."""
    finished_at = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO pipeline_health
          (run_kind, started_at, finished_at, status,
           ads_checked, changes, errors, error_msg, since_arg, limit_arg)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (run_kind, started_at, finished_at, status,
          ads_checked, changes, errors, error_msg, since_arg, limit_arg))
    conn.commit()


def _adv_ids_to_refresh(conn, args) -> list[str]:
    """Return distinct advertiser_ids whose ads haven't been status-checked
    recently (or all of them if --all). Returned as TEXT strings (as stored)."""
    if args.all:
        rows = conn.execute(
            "SELECT DISTINCT advertiser_id FROM tiktok_ads WHERE advertiser_id IS NOT NULL"
        ).fetchall()
    else:
        cutoff = (datetime.now(timezone.utc) - _parse_duration(args.since)).isoformat()
        rows = conn.execute(
            """SELECT DISTINCT advertiser_id FROM tiktok_ads
               WHERE advertiser_id IS NOT NULL
                 AND (last_status_check IS NULL OR last_status_check < ?)""",
            (cutoff,)
        ).fetchall()
    ids = [r[0] for r in rows if r[0]]
    if args.limit:
        ids = ids[:args.limit]
    return ids


def refresh(args):
    if not t.CLIENT_KEY or not t.CLIENT_SECRET:
        sys.exit("ERROR: TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET missing from .env")

    # Acquire exclusive write lock on this DB file before any UPDATE/INSERT.
    # Prevents concurrent corruption between this script and discover_*.py
    # if both happen to run against the same DB (e.g. when POLITICIAN_ADS_DB
    # points at the local master rather than the deploy public DB).
    from db_lock import db_lock
    with db_lock(DB):
        _refresh_impl(args)


def _refresh_impl(args):
    started_at = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    # Counters for the heartbeat row written at the end (or on failure)
    n_changed_total   = 0
    n_unchanged_total = 0
    n_failed_total    = 0
    crash_msg         = None
    t.reset_api_metrics()
    try:
        n_changed_total, n_unchanged_total, n_failed_total = \
            _refresh_loop(args, conn)
    except Exception as e:
        crash_msg = repr(e)
        raise
    finally:
        api_summary = t.print_api_summary('refresh')
        # Tuck the API summary into the heartbeat's error_msg so the
        # dashboard can show it even on successful runs (no schema change
        # needed). On failure, prefer the crash message.
        msg = crash_msg if crash_msg else (api_summary or None)
        record_health(
            conn,
            run_kind='refresh',
            started_at=started_at,
            status='failed' if crash_msg else 'ok',
            ads_checked=n_changed_total + n_unchanged_total + n_failed_total,
            changes=n_changed_total,
            errors=n_failed_total,
            error_msg=msg,
            since_arg=getattr(args, 'since', None),
            limit_arg=getattr(args, 'limit', None),
        )
        conn.close()


def _is_ci() -> bool:
    """True when running inside GitHub Actions (or any env that sets this)."""
    return os.environ.get('GITHUB_ACTIONS', '').lower() == 'true'


def _checkpoint_push(label: str) -> None:
    """Commit + push the current DB to origin/main so a runner-timeout
    doesn't discard in-flight refresh progress. CI-only — a no-op locally.
    Best-effort: any failure (no changes, push conflict, missing config)
    is logged but never raises, because losing a checkpoint is strictly
    better than losing the whole refresh by aborting on it.

    Git identity is passed via env vars rather than `git config` so we
    don't pollute the runner's global config (which the workflow's later
    'Commit + push' step sets itself).
    """
    if not _is_ci():
        return
    env = {
        **os.environ,
        'GIT_AUTHOR_NAME':     'github-actions[bot]',
        'GIT_AUTHOR_EMAIL':    'github-actions[bot]@users.noreply.github.com',
        'GIT_COMMITTER_NAME':  'github-actions[bot]',
        'GIT_COMMITTER_EMAIL': 'github-actions[bot]@users.noreply.github.com',
    }
    try:
        # Skip if nothing actually changed on disk (avoids empty commits
        # when the first batch of N ads happened to all be unchanged).
        diff = subprocess.run(['git', 'diff', '--quiet', '--', DB],
                              capture_output=True, timeout=10)
        if diff.returncode == 0:
            return
        # Per-call timeouts so a network hang on `git push` (TikTok ads
        # API can be slow; GitHub's git proxy occasionally is too) doesn't
        # freeze the refresh loop. 60s for push covers a slow upload of
        # the ~1 MB DB; add/commit are local so 30s is generous.
        subprocess.run(['git', 'add', DB], env=env, check=True,
                       capture_output=True, timeout=30)
        subprocess.run(
            ['git', 'commit', '-m', f'auto: refresh checkpoint — {label}'],
            env=env, check=True, capture_output=True, timeout=30,
        )
        # HEAD:main is explicit — actions/checkout@v4 leaves us on the
        # 'main' branch on push-triggered workflows but a future change in
        # checkout could shift to detached HEAD, breaking `push origin HEAD`.
        subprocess.run(['git', 'push', 'origin', 'HEAD:main'],
                       env=env, check=True, capture_output=True, timeout=60)
        print(f"  ✓ checkpoint pushed: {label}", flush=True)
    except subprocess.TimeoutExpired as e:
        print(f"  ⚠ checkpoint timed out on {e.cmd[1] if len(e.cmd) > 1 else '?'} "
              f"({e.timeout}s) — continuing refresh", flush=True)
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b'').decode('utf-8', 'replace')[:300]
        print(f"  ⚠ checkpoint failed ({e.returncode}): {stderr}", flush=True)
    except Exception as e:
        print(f"  ⚠ checkpoint exception: {e!r}", flush=True)


def _refresh_loop(args, conn):
    """Per-advertiser refresh via /ad/query/ bulk endpoint.

    Returns (n_changed, n_unchanged, n_adv_errors):
      n_changed     — ads whose status/statement changed this run
      n_unchanged   — ads confirmed unchanged
      n_adv_errors  — advertisers whose API call failed (ads inside skipped)

    Uses /ad/query/ (not /ad/detail/) to avoid the low daily quota on the
    per-ad detail endpoint. One /ad/query/ page covers up to 50 ads; ~74
    known advertisers need ~2 pages each = ~150 API calls total per run,
    vs the old 200+ calls to the rate-limited /ad/detail/ endpoint.
    """
    from datetime import timedelta as _td, date as _date
    since_date = (_date.today() - _td(days=REFRESH_SINCE_DAYS)).strftime('%Y-%m-%d')

    adv_ids = _adv_ids_to_refresh(conn, args)
    print(f"  advertisers to refresh: {len(adv_ids)} (since_date={since_date})",
          flush=True)
    if not adv_ids:
        print("  nothing to do.")
        return (0, 0, 0)

    # Snapshot current DB state for O(1) lookups during the loop.
    existing: dict[str, dict] = {}
    for row in conn.execute("""SELECT ad_id, ad_status, status_statement,
                                      advertiser_id,
                                      advertiser_disclosed_name AS handle
                               FROM tiktok_ads"""):
        existing[row['ad_id']] = dict(row)

    t.get_access_token()

    n_changed, n_unchanged, n_adv_errors = 0, 0, 0

    for i, adv_id in enumerate(adv_ids, 1):
        try:
            biz_id = int(adv_id)
        except (TypeError, ValueError):
            print(f"  ⚠ non-numeric advertiser_id {adv_id!r} — skipped", flush=True)
            n_adv_errors += 1
            continue

        try:
            items = t.query_ads_for_advertiser(biz_id, since_date)
        except t.RateLimitExceeded:
            print(f"  [429] quota hit at advertiser {i}/{len(adv_ids)} — stopping.",
                  flush=True)
            break
        except Exception as e:
            print(f"  ✗ advertiser {adv_id}: {type(e).__name__}: {e}", flush=True)
            n_adv_errors += 1
            continue

        now = datetime.now(timezone.utc).isoformat()
        for item in items:
            ad_obj = item.get('ad', {}) or {}
            ad_id  = str(ad_obj.get('id') or '')
            if not ad_id:
                continue

            row = existing.get(ad_id)
            if row is None:
                continue  # not in our public DB (maybe stripped)

            new_status = ad_obj.get('status') or 'unknown'
            new_stmt   = ad_obj.get('status_statement') or ''
            prev_status = row.get('ad_status') or 'unknown'
            prev_stmt   = row.get('status_statement') or ''
            handle      = row.get('handle') or ''

            # Substrings that indicate TikTok enforcement in status_statement.
            # Kept broad so wording changes ("permanently removed", "policy
            # violation", "terms of service") are still caught. If TikTok
            # adds new phrasing, extend this tuple.
            TAKEDOWN_SIGNALS = (
                'removed', 'violation', 'deleted', 'expired',
                'policy', 'terms', 'prohibited', 'banned', 'suspended',
                'enforcement', 'takedown',
            )
            prev_signal = any(s in prev_stmt.lower() for s in TAKEDOWN_SIGNALS)
            new_signal  = any(s in new_stmt.lower()  for s in TAKEDOWN_SIGNALS)
            is_real_change = (new_status != prev_status) or (new_signal != prev_signal)

            if is_real_change:
                conn.execute("""INSERT INTO tiktok_ad_status_changes
                    (ad_id, observed_at, prev_status, new_status,
                     prev_statement, new_statement, advertiser_id, handle)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ad_id, now, prev_status, new_status,
                     prev_stmt, new_stmt, adv_id, handle))
                print(f"  ⚡ {ad_id}  @{handle:<25}  "
                      f"{prev_status} -> {new_status}  "
                      f"({new_stmt[:60]})", flush=True)
                n_changed += 1
                # Update snapshot so subsequent advertisers' dupe-ad checks work
                existing[ad_id]['ad_status']      = new_status
                existing[ad_id]['status_statement'] = new_stmt
            else:
                n_unchanged += 1

            # Always bump last_status_check + refresh status/statement
            conn.execute("""UPDATE tiktok_ads
                SET ad_status=?, status_statement=?, last_status_check=?
                WHERE ad_id=?""",
                (new_status, new_stmt, now, ad_id))

        conn.commit()

        if i % 10 == 0:
            print(f"  [{i}/{len(adv_ids)}] advertisers done  "
                  f"changes={n_changed}  unchanged={n_unchanged}  "
                  f"adv_errors={n_adv_errors}", flush=True)

        # CI checkpoint every CHECKPOINT_EVERY advertisers
        if i % CHECKPOINT_EVERY == 0:
            _checkpoint_push(
                f'{i}/{len(adv_ids)} advertisers '
                f'(changes={n_changed}, errors={n_adv_errors})'
            )

    print(f"\n  ── done ──  changed: {n_changed}  unchanged: {n_unchanged}"
          f"  adv_errors: {n_adv_errors}", flush=True)
    return (n_changed, n_unchanged, n_adv_errors)


def _parse_duration(s: str) -> timedelta:
    """'24h' / '7d' / '30m' → timedelta"""
    unit = s[-1].lower()
    n = int(s[:-1])
    return {'h': timedelta(hours=n), 'd': timedelta(days=n),
            'm': timedelta(minutes=n)}.get(unit, timedelta(hours=24))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--all', action='store_true', help='Force refresh every ad')
    ap.add_argument('--since', default='24h',
                    help='Refresh ads not checked in this duration (default 24h)')
    ap.add_argument('--limit', type=int, default=0, help='Cap API calls')
    args = ap.parse_args()
    refresh(args)


if __name__ == '__main__':
    main()
