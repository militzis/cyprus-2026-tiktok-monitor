"""Export a CSV shaped for TikTok's bulk URL reporter form.

Form fields (from TikTok's reporter interface, May 2026):
  URL to be reported         (required)
  Category of URL            (required, dropdown)
  Report reason              (required, dropdown)
  Additional details         (optional, free text)

The CSV produced here has matching columns. Paste each row into the form
manually, or — if TikTok publishes a CSV-upload API for the bulk reporter —
upload the file directly.

We fill `URL to be reported` with the Ad Library URL
(https://library.tiktok.com/ads/detail/?ad_id=...), since that is the only
URL TikTok itself exposes for the ad. The canonical post URL on tiktok.com
(/@handle/video/<id>) is *deliberately stripped* from the Ad Library page,
so we cannot fill it without scraping the candidate's full profile feed
and matching by date — which is fragile at scale. See the email at
docs/email_tiktok_post_id_gap.md for the full rationale.

Usage:
  python export_bulk_report.py                       # all candidates + party accounts
  python export_bulk_report.py --include-supporters  # also include party supporters
  python export_bulk_report.py --output reports/bulk_report.csv
"""
import os, sys, csv, sqlite3, argparse
from datetime import date
sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None

DB = os.environ.get('POLITICIAN_ADS_DB',
                    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 'politician_ads_public.db'))

# These match the TikTok bulk reporter's dropdown options (suggested defaults).
# If TikTok rejects your category, try one of the alternatives.
DEFAULT_CATEGORY = "Misleading information"
DEFAULT_REASON   = "Political advertising prohibited by TikTok policy"
# Alternative categories you can try if the above is rejected:
#   "Spam / scam", "Misleading content", "Other policy violation"


def comment_for(row: dict) -> str:
    """Build the 'Additional details' free-text comment that explains the
       violation in plain language, ready for the reporter form."""
    parts = []
    if row['matched_candidate']:
        cand_line = f"Candidate: {row['matched_candidate']}"
        if row['matched_party']:
            cand_line += f" ({row['matched_party']}"
            if row['matched_district']:
                cand_line += f", {row['matched_district']}"
            cand_line += ")"
        parts.append(cand_line)
    elif row['matched_party']:
        parts.append(f"Party-aligned account: {row['matched_party']}")
    parts.append("Cyprus 2026 parliamentary elections — paid political advertising")
    parts.append("(TikTok prohibits political advertising globally per Community Guidelines)")
    if row['first_shown']:
        date_line = f"Ad first shown: {row['first_shown']}"
        if row['last_shown']:
            date_line += f"; last shown: {row['last_shown']}"
        parts.append(date_line)
    if row['handle']:
        parts.append(f"Advertiser handle: @{row['handle']}")
    parts.append(f"Detected via TikTok Research API ad_id={row['ad_id']}")
    return " · ".join(parts)


def export(args):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    reportable_types = ['manual_resume', 'party_account', 'party_coordinator',
                        'political_movement', 'politician_non_candidate']
    if args.include_supporters:
        reportable_types += ['party_supporter']
    if args.include_media:
        reportable_types += ['commentator', 'news_outlet', 'podcast', 'satirist']

    placeholders = ','.join('?' for _ in reportable_types)
    rows = conn.execute(f"""
        SELECT advertiser_disclosed_name AS handle, advertiser_id,
               matched_candidate, matched_party, matched_district,
               ad_id, ad_url, ad_status, first_shown, last_shown,
               match_type, reach_raw
        FROM tiktok_ads
        WHERE match_type IN ({placeholders})
          AND ad_url IS NOT NULL
        ORDER BY matched_party, matched_candidate, first_shown
    """, reportable_types).fetchall()
    print(f"  rows to export: {len(rows)}")

    out_path = args.output or f"bulk_report_tiktok_{date.today().isoformat()}.csv"
    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
    with open(out_path, 'w', encoding='utf-8-sig', newline='') as f:
        # utf-8-sig = UTF-8 with BOM, so Excel opens it with Greek characters
        # correctly without a manual import-encoding step.
        w = csv.writer(f)
        w.writerow(['URL to be reported', 'Category of URL', 'Report reason',
                    'Additional details'])
        for r in rows:
            d = dict(r)
            w.writerow([
                d['ad_url'],
                DEFAULT_CATEGORY,
                DEFAULT_REASON,
                comment_for(d),
            ])
    print(f"  ✓ saved: {out_path}")

    # Per-party summary
    print(f"\n  Breakdown by party:")
    by_party = {}
    for r in rows:
        p = r['matched_party'] or '(no party)'
        by_party[p] = by_party.get(p, 0) + 1
    for p, n in sorted(by_party.items(), key=lambda x: -x[1]):
        print(f"    {n:>4}  {p}")

    conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', default='', help='Output CSV path')
    ap.add_argument('--include-supporters', action='store_true',
                    help='Also include party_supporter accounts (not on the ballot but boosting party content)')
    ap.add_argument('--include-media', action='store_true',
                    help='Also include commentator/news/podcast/satirist accounts')
    args = ap.parse_args()
    export(args)


if __name__ == '__main__':
    main()
