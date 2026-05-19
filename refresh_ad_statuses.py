"""Refresh ad statuses by re-querying TikTok's /v2/research/adlib/ad/detail/ endpoint.

For every ad in the DB:
  1. Call ad/detail/ with the ad_id
  2. Compare the returned `status` + `status_statement` to what we have
  3. If different (or this is the first check), INSERT a row into
     tiktok_ad_status_changes and UPDATE the canonical ad_status / status_statement
  4. Bump last_status_check timestamp

Status values seen so far from TikTok's API:
  - "active"               — ad is currently being shown
  - "inactive"             — advertiser stopped or budget exhausted
  - "removed_by_tiktok"    — TikTok removed it (policy violation)
  - "deleted_by_advertiser"
  - "expired"

Usage:
  python refresh_ad_statuses.py                  # refresh ads not checked in 24h
  python refresh_ad_statuses.py --all            # force-refresh everything
  python refresh_ad_statuses.py --since 7d       # refresh ads not checked in 7 days
  python refresh_ad_statuses.py --limit 200      # cap API calls (rate-limit safety)
"""
import os, sys, time, sqlite3, argparse, json, subprocess
from datetime import datetime, timedelta
sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discover_tiktok_ads as t

DB = os.environ.get('POLITICIAN_ADS_DB',
                    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 'politician_ads_public.db'))
AD_DETAIL_URL = f"{t.API_BASE}/v2/research/adlib/ad/detail/"

# How often to git-commit + push the in-progress DB inside CI. The 2026-05-19
# 22:01 UTC election-week run was killed at the 45min wall mid-refresh; ALL
# the work it had done was lost because the workflow's commit step never ran.
# Pushing every N ads turns a runner-timeout into "we kept the first ~N ads
# of progress" instead of "we lost everything."
CHECKPOINT_EVERY = 50


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
    finished_at = datetime.utcnow().isoformat()
    conn.execute("""
        INSERT INTO pipeline_health
          (run_kind, started_at, finished_at, status,
           ads_checked, changes, errors, error_msg, since_arg, limit_arg)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (run_kind, started_at, finished_at, status,
          ads_checked, changes, errors, error_msg, since_arg, limit_arg))
    conn.commit()


def fetch_ad_detail(ad_id: str) -> dict | None:
    """Call /v2/research/adlib/ad/detail/ for a single ad_id."""
    body = {'ad_id': ad_id}
    fields = ','.join([
        'ad.id', 'ad.status', 'ad.status_statement',
        'ad.first_shown_date', 'ad.last_shown_date',
        'advertiser.business_id', 'advertiser.business_name',
    ])
    try:
        data = t._api_post(AD_DETAIL_URL, {'fields': fields}, body)
    except Exception as e:
        print(f"  ✗ ad {ad_id}: {e}")
        return None
    return (data.get('data') or {}).get('ad') or {}


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
    started_at = datetime.utcnow().isoformat()
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    # Counters for the heartbeat row written at the end (or on failure)
    n_changed_total   = 0
    n_unchanged_total = 0
    n_failed_total    = 0
    crash_msg         = None
    try:
        n_changed_total, n_unchanged_total, n_failed_total = \
            _refresh_loop(args, conn)
    except Exception as e:
        crash_msg = repr(e)
        raise
    finally:
        record_health(
            conn,
            run_kind='refresh',
            started_at=started_at,
            status='failed' if crash_msg else 'ok',
            ads_checked=n_changed_total + n_unchanged_total + n_failed_total,
            changes=n_changed_total,
            errors=n_failed_total,
            error_msg=crash_msg,
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
                              capture_output=True)
        if diff.returncode == 0:
            return
        subprocess.run(['git', 'add', DB], env=env, check=True,
                       capture_output=True)
        subprocess.run(
            ['git', 'commit', '-m', f'auto: refresh checkpoint — {label}'],
            env=env, check=True, capture_output=True,
        )
        subprocess.run(['git', 'push', 'origin', 'HEAD'],
                       env=env, check=True, capture_output=True)
        print(f"  ✓ checkpoint pushed: {label}", flush=True)
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b'').decode('utf-8', 'replace')[:300]
        print(f"  ⚠ checkpoint failed ({e.returncode}): {stderr}", flush=True)
    except Exception as e:
        print(f"  ⚠ checkpoint exception: {e!r}", flush=True)


def _refresh_loop(args, conn):
    """Inner loop. Returns (n_changed, n_unchanged, n_failed)."""

    # Pick which ads to refresh
    if args.all:
        sel = "SELECT ad_id, advertiser_id, advertiser_disclosed_name AS handle, ad_status, status_statement FROM tiktok_ads"
        params = ()
    else:
        # Default: ads NEVER checked, OR checked more than `since` ago
        cutoff = (datetime.utcnow() - _parse_duration(args.since)).isoformat()
        sel = """SELECT ad_id, advertiser_id, advertiser_disclosed_name AS handle,
                        ad_status, status_statement
                 FROM tiktok_ads
                 WHERE last_status_check IS NULL OR last_status_check < ?"""
        params = (cutoff,)

    rows = conn.execute(sel, params).fetchall()
    if args.limit:
        rows = rows[:args.limit]

    print(f"  candidates to refresh: {len(rows)}")
    if not rows:
        print("  nothing to do."); return (0, 0, 0)

    t.get_access_token()

    n_changed = 0
    n_unchanged = 0
    n_failed = 0
    for i, r in enumerate(rows, 1):
        if i % 25 == 0:
            print(f"  [{i}/{len(rows)}] changes so far: {n_changed}, unchanged: {n_unchanged}, errors: {n_failed}")
        detail = fetch_ad_detail(r['ad_id'])
        if detail is None:
            n_failed += 1
            continue

        new_status   = detail.get('status') or 'unknown'
        new_stmt     = detail.get('status_statement')
        prev_status  = r['ad_status'] or 'unknown'
        prev_stmt    = r['status_statement']

        now = datetime.utcnow().isoformat()
        # Log to tiktok_ad_status_changes ONLY for meaningful transitions:
        #   - ad_status itself changed (active→inactive, etc.), OR
        #   - status_statement gained a takedown signal that wasn't there
        #     before (the words derive_status() actually looks at)
        # Statement-only diffs of N/A ↔ N/A or "advertiser_account_deleted..." ↔
        # N/A (from prior bookkeeping markers) used to flood the log with
        # active→active rows — 22 noise rows in a single run earlier today.
        prev_stmt_l = (prev_stmt or '').lower()
        new_stmt_l  = (new_stmt or '').lower()
        TAKEDOWN_SIGNALS = ('removed', 'violation', 'deleted', 'expired')
        prev_signal = any(s in prev_stmt_l for s in TAKEDOWN_SIGNALS)
        new_signal  = any(s in new_stmt_l for s in TAKEDOWN_SIGNALS)
        is_real_change = (new_status != prev_status) or (new_signal != prev_signal)

        if is_real_change:
            conn.execute("""INSERT INTO tiktok_ad_status_changes
                            (ad_id, observed_at, prev_status, new_status,
                             prev_statement, new_statement, advertiser_id, handle)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                         (r['ad_id'], now, prev_status, new_status,
                          prev_stmt, new_stmt, r['advertiser_id'], r['handle']))
            conn.execute("""UPDATE tiktok_ads
                            SET ad_status=?, status_statement=?, last_status_check=?
                            WHERE ad_id=?""",
                         (new_status, new_stmt, now, r['ad_id']))
            print(f"  ⚡ {r['ad_id']}  @{r['handle'] or '?':<25}  "
                  f"{prev_status} → {new_status}  "
                  f"({(new_stmt or '')[:60]})")
            n_changed += 1
        else:
            # Still update ad_status + status_statement if the API has a fresher
            # value — just don't insert a row into the changes log.
            conn.execute("""UPDATE tiktok_ads
                            SET ad_status=?, status_statement=?, last_status_check=?
                            WHERE ad_id=?""",
                         (new_status, new_stmt, now, r['ad_id']))
            n_unchanged += 1

        conn.commit()
        # Light throttle to avoid hammering the API
        time.sleep(getattr(t, 'REQUEST_DELAY', 0.5))

        # CI checkpoint: every CHECKPOINT_EVERY ads, commit + push the DB
        # state so a runner-timeout doesn't waste the work we've already
        # done. Local-dev no-op (gated on GITHUB_ACTIONS env var).
        if i % CHECKPOINT_EVERY == 0:
            _checkpoint_push(
                f'{i}/{len(rows)} ads (changes={n_changed}, errors={n_failed})'
            )

    # Don't close conn here — _refresh_impl owns it (so the heartbeat
    # row can be written in the finally block before close).
    print(f"\n  ── done ──  changed: {n_changed}  unchanged: {n_unchanged}  errors: {n_failed}")
    return (n_changed, n_unchanged, n_failed)


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
