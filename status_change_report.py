"""Generate a daily/weekly report of TikTok ad status changes.

Reads from tiktok_ad_status_changes (populated by refresh_ad_statuses.py)
and produces a markdown report grouped by:
  - new_status (active → removed_by_tiktok is the headline)
  - candidate / party

Usage:
  python status_change_report.py                       # last 24h
  python status_change_report.py --since 7d            # last 7 days
  python status_change_report.py --since 2026-05-10    # since a specific date
  python status_change_report.py --output reports/today.md
"""
import os, sys, sqlite3, argparse
from datetime import datetime, timedelta
from collections import defaultdict, Counter
sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None

DB = os.environ.get('POLITICIAN_ADS_DB',
                    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 'politician_ads_public.db'))

HEADLINE_TRANSITIONS = {
    ('active', 'removed_by_tiktok'): "🚨 REMOVED BY TIKTOK",
    ('active', 'deleted_by_advertiser'): "🗑 deleted by advertiser",
    ('active', 'inactive'): "⏸ stopped running",
    ('active', 'expired'): "⌛ expired",
    ('inactive', 'active'): "▶ resumed",
    ('removed_by_tiktok', 'active'): "✨ restored after removal",
}


def parse_since(s: str) -> str:
    """Parse '7d' / '24h' / '2026-05-10' into an ISO-format cutoff."""
    if s and s[-1].lower() in ('h', 'd', 'm'):
        n = int(s[:-1])
        unit = s[-1].lower()
        delta = {'h': timedelta(hours=n), 'd': timedelta(days=n),
                 'm': timedelta(minutes=n)}[unit]
        return (datetime.utcnow() - delta).isoformat()
    # Assume YYYY-MM-DD
    return datetime.fromisoformat(s).isoformat()


def gen(args):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    cutoff = parse_since(args.since)
    rows = conn.execute("""
        SELECT sc.*, a.matched_candidate, a.matched_party, a.matched_district,
               a.ad_url, a.first_shown, a.last_shown
        FROM tiktok_ad_status_changes sc
        LEFT JOIN tiktok_ads a USING(ad_id)
        WHERE sc.observed_at >= ?
        ORDER BY sc.observed_at DESC
    """, (cutoff,)).fetchall()

    print(f"  status changes since {cutoff[:10]}: {len(rows)}")
    if not rows:
        print("  no changes to report.")
        return

    # Group
    by_transition = defaultdict(list)
    for r in rows:
        key = (r['prev_status'] or 'unknown', r['new_status'] or 'unknown')
        by_transition[key].append(r)

    # Build markdown
    lines = [
        f"# TikTok ad status changes — Cyprus 2026 monitor",
        f"",
        f"**Window**: since {cutoff[:10]}    **Total changes**: {len(rows)}",
        f"",
        f"## Summary",
        f"",
        f"| Transition | Count | |",
        f"|---|---|---|",
    ]
    for (prev, new), ads in sorted(by_transition.items(), key=lambda x: -len(x[1])):
        label = HEADLINE_TRANSITIONS.get((prev, new), f"{prev} → {new}")
        lines.append(f"| `{prev}` → `{new}` | **{len(ads)}** | {label} |")
    lines.append("")

    # Detail sections
    for (prev, new), ads in sorted(by_transition.items(), key=lambda x: -len(x[1])):
        label = HEADLINE_TRANSITIONS.get((prev, new), f"{prev} → {new}")
        lines.append(f"## {label} — `{prev}` → `{new}` ({len(ads)} ads)")
        lines.append("")
        lines.append("| When (UTC) | Handle | Candidate | Party | District | Ad | Reason |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in ads:
            when = r['observed_at'][:16].replace('T', ' ')
            handle = f"`@{r['handle'] or '?'}`"
            cand = r['matched_candidate'] or '—'
            party = r['matched_party'] or '—'
            dist = r['matched_district'] or '—'
            ad_link = f"[lib]({r['ad_url']})" if r['ad_url'] else r['ad_id']
            stmt = (r['new_statement'] or '').replace('|', '\\|')[:80]
            lines.append(f"| {when} | {handle} | {cand} | {party} | {dist} | {ad_link} | {stmt} |")
        lines.append("")

    # Candidate-level rollup of removals
    removed_by_cand = Counter()
    for r in rows:
        if r['new_status'] == 'removed_by_tiktok' and r['matched_candidate']:
            removed_by_cand[r['matched_candidate']] += 1
    if removed_by_cand:
        lines.append(f"## 🚨 Removals by TikTok — top candidates")
        lines.append("")
        lines.append("| Candidate | # Ads removed |")
        lines.append("|---|---|")
        for cand, n in removed_by_cand.most_common(20):
            lines.append(f"| {cand} | {n} |")
        lines.append("")

    output = '\n'.join(lines)
    if args.output:
        os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(output)
        print(f"\n  ✓ report written: {args.output}")
    else:
        print(output)

    conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--since', default='24h',
                    help='Time window: 24h, 7d, or a date YYYY-MM-DD (default 24h)')
    ap.add_argument('--output', default='',
                    help='Output markdown file path (omit for stdout)')
    args = ap.parse_args()
    gen(args)


if __name__ == '__main__':
    main()
