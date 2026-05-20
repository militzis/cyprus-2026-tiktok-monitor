"""export_tiktok_daily_csv.py — export the TikTok status-change log +
summary as CSVs, with date-stamped filenames that never overwrite.

This is the deploy/CI copy. Identical in shape to the OneDrive version
the user runs locally for ad-hoc exports, but with env-var-driven
defaults so it works on a GitHub Actions runner (which has no OneDrive
folder and no master DB). Two CSVs per run:

  reports/daily/tiktok_changes_YYYY-MM-DD.csv   — row per status change
  reports/daily/tiktok_summary_YYYY-MM-DD.csv   — aggregated counts

Defaults (CI-friendly):
  DB     = $POLITICIAN_ADS_DB or politician_ads_public.db (cwd)
  OUT    = $TIKTOK_REPORT_DIR or reports/daily (cwd)
  WINDOW = 24h

The daily workflow runs this AFTER status_change_report.py (markdown
version) and BEFORE the strip + commit steps, so the CSVs land in the
same commit as the markdown report. `git pull origin main` brings both
formats to your local clone — markdown for browsing, CSV for analysis.
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None

BASE = os.path.dirname(os.path.abspath(__file__))

DB_DEFAULT  = os.environ.get('POLITICIAN_ADS_DB',
                             os.path.join(BASE, 'politician_ads_public.db'))
OUT_DEFAULT = os.environ.get('TIKTOK_REPORT_DIR',
                             os.path.join(BASE, 'reports', 'daily'))

# Same labels as status_change_report.py (the markdown version) so the
# two reports tell the same story.
HEADLINE_TRANSITIONS = {
    ('active', 'removed_by_tiktok'):     'REMOVED BY TIKTOK',
    ('active', 'deleted_by_advertiser'): 'deleted by advertiser',
    ('active', 'inactive'):              'stopped running',
    ('active', 'expired'):               'expired',
    ('inactive', 'active'):              'resumed',
    ('removed_by_tiktok', 'active'):     'restored after removal',
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_since(s: str) -> str:
    """'24h' / '7d' / '30m' / '2026-05-15' → ISO cutoff."""
    if s and s[-1].lower() in ('h', 'd', 'm'):
        n = int(s[:-1])
        unit = s[-1].lower()
        delta = {'h': timedelta(hours=n),
                 'd': timedelta(days=n),
                 'm': timedelta(minutes=n)}[unit]
        return (_now_utc() - delta).isoformat()
    return datetime.fromisoformat(s).isoformat()


def _unique_path(base: str) -> str:
    """Refuse to overwrite — if `base` exists, append _HHMM (and _HHMMSS
    if even that collides). User explicitly asked for this so a second
    run the same day keeps both copies."""
    if not os.path.exists(base):
        return base
    stem, ext = os.path.splitext(base)
    candidate = f"{stem}{_now_utc().strftime('_%H%M')}{ext}"
    if os.path.exists(candidate):
        candidate = f"{stem}{_now_utc().strftime('_%H%M%S')}{ext}"
    return candidate


def _write_changes_csv(rows, path: str) -> None:
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow([
            'observed_at', 'handle', 'candidate', 'party', 'district',
            'match_type', 'ad_id', 'prev_status', 'new_status',
            'transition_label', 'new_statement',
            'reach_upper', 'estimated_eur_mid',
            'first_shown', 'last_shown', 'ad_url',
        ])
        for r in rows:
            label = HEADLINE_TRANSITIONS.get(
                (r['prev_status'] or '', r['new_status'] or ''), '')
            w.writerow([
                (r['observed_at'] or '')[:19].replace('T', ' '),
                r['handle'] or '',
                r['matched_candidate'] or '',
                r['matched_party'] or '',
                r['matched_district'] or '',
                r['match_type'] or '',
                r['ad_id'],
                r['prev_status'] or '',
                r['new_status'] or '',
                label,
                (r['new_statement'] or '')[:200],
                r['times_shown_upper_bound'] if r['times_shown_upper_bound'] is not None else '',
                r['estimated_spend_eur_mid']  if r['estimated_spend_eur_mid']  is not None else '',
                r['first_shown'] or '',
                r['last_shown']  or '',
                r['ad_url']      or '',
            ])


def _write_summary_csv(rows, cutoff: str, path: str) -> None:
    by_transition        = Counter()
    by_candidate_removed = Counter()
    by_party_removed     = Counter()
    for r in rows:
        prev = r['prev_status'] or 'unknown'
        new  = r['new_status']  or 'unknown'
        by_transition[(prev, new)] += 1
        if new == 'removed_by_tiktok':
            if r['matched_candidate']:
                by_candidate_removed[r['matched_candidate']] += 1
            if r['matched_party']:
                by_party_removed[r['matched_party']] += 1

    cand_to_party = {r['matched_candidate']: r['matched_party']
                     for r in rows
                     if r['new_status'] == 'removed_by_tiktok' and r['matched_candidate']}

    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['== Window =='])
        w.writerow(['cutoff_utc', cutoff[:19]])
        w.writerow(['generated_utc', _now_utc().isoformat()[:19]])
        w.writerow(['total_changes', len(rows)])
        w.writerow([])
        w.writerow(['== Transitions =='])
        w.writerow(['prev_status', 'new_status', 'count', 'label'])
        for (prev, new), n in by_transition.most_common():
            w.writerow([prev, new, n,
                        HEADLINE_TRANSITIONS.get((prev, new), '')])
        w.writerow([])
        w.writerow(['== TikTok removals - by candidate =='])
        w.writerow(['candidate', 'party', 'removed_count'])
        for cand, n in by_candidate_removed.most_common():
            w.writerow([cand, cand_to_party.get(cand, ''), n])
        w.writerow([])
        w.writerow(['== TikTok removals - by party =='])
        w.writerow(['party', 'removed_count'])
        for party, n in by_party_removed.most_common():
            w.writerow([party, n])


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument('--since', default='24h',
                   help='Time window: 24h, 7d, 30m, or YYYY-MM-DD (default: 24h)')
    p.add_argument('--db', default=DB_DEFAULT,
                   help='Path to public DB (default: $POLITICIAN_ADS_DB or '
                        'cwd/politician_ads_public.db)')
    p.add_argument('--out', default=OUT_DEFAULT,
                   help='Output folder (default: $TIKTOK_REPORT_DIR or '
                        'cwd/reports/daily)')
    args = p.parse_args()

    if not os.path.exists(args.db):
        # In CI this would be unusual but we don't want to abort the whole
        # workflow; print and exit 0 so subsequent steps still run.
        print(f"  ⚠ DB not found: {args.db} — skipping CSV export.")
        return 0
    os.makedirs(args.out, exist_ok=True)

    cutoff = _parse_since(args.since)
    today  = _now_utc().strftime('%Y-%m-%d')

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT sc.observed_at, sc.ad_id, sc.handle,
               sc.prev_status, sc.new_status,
               sc.prev_statement, sc.new_statement,
               a.matched_candidate, a.matched_party, a.matched_district,
               a.match_type, a.ad_url, a.first_shown, a.last_shown,
               a.times_shown_upper_bound, a.estimated_spend_eur_mid
        FROM tiktok_ad_status_changes sc
        LEFT JOIN tiktok_ads a USING(ad_id)
        WHERE sc.observed_at >= ?
        ORDER BY sc.observed_at DESC
    """, (cutoff,)).fetchall()
    conn.close()

    if not rows:
        print(f"  No status changes in window (since {cutoff[:19]} UTC). "
              f"Skipping CSV write.")
        return 0

    changes_path = _unique_path(os.path.join(args.out, f'tiktok_changes_{today}.csv'))
    summary_path = _unique_path(os.path.join(args.out, f'tiktok_summary_{today}.csv'))
    _write_changes_csv(rows, changes_path)
    _write_summary_csv(rows, cutoff, summary_path)

    print(f"  ✓ {len(rows)} status changes since {cutoff[:19]} UTC")
    print(f"  ✓ wrote: {changes_path}")
    print(f"  ✓ wrote: {summary_path}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
