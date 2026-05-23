"""auto_review.py — Claude-API second-opinion review of TikTok advertiser
classifications.

Solves the recurring class of bug we hit today (@petrouiakovos /
@champis_me_p / @ttoppouzi / @marioshaperis were all misclassified as
candidates/political and only caught by chance). Each promotion is now
auto-reviewed against the bio + ad transcripts, and the dashboard
surfaces any disagreement between Claude's verdict and our current tier.

Outputs are persisted to four new columns on tiktok_ads:
  auto_review_verdict     TEXT   — 'candidate' | 'supporter' | 'commentator' |
                                   'party_account' | 'news_outlet' |
                                   'fp_business' | 'fp_personal' | 'unclear'
  auto_review_confidence  REAL   — 0.0–1.0
  auto_review_reason      TEXT   — one-sentence explanation
  auto_review_at          TEXT   — ISO timestamp

The verdict is per-AD (we copy it to every ad of the same handle since
they share the same advertiser identity). This makes the dashboard query
trivial: just SELECT MAX(auto_review_at) per handle.

Usage:
    python auto_review.py --handle florentzos_karayiannas
    python auto_review.py --bucket recent_promotions  # all handles promoted in last 14d
    python auto_review.py --bucket lower_confidence   # commentator/satirist/etc. w/ reach >=10K
    python auto_review.py --bucket all_unreviewed     # everything that's never been auto-reviewed
    python auto_review.py --bucket all_unreviewed --limit 20

Exit codes:
  0  success
  1  no candidates found (or empty bucket)
  2  bad args / missing API key
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None
load_dotenv(override=True)

# Use the master DB by default so the verdict applies to everything, even
# ads that aren't in the public snapshot (e.g. content_keyword handles
# that we may want to auto-promote later).
DB = os.environ.get('POLITICIAN_ADS_DB',
                    r'C:\Users\milit\meta_pipeline_data\politician_ads.db')

VALID_VERDICTS = {
    'candidate', 'supporter', 'commentator', 'party_account',
    'news_outlet', 'fp_business', 'fp_personal', 'unclear',
}

# Schema additions — created on first run
NEW_COLUMNS = [
    ('auto_review_verdict',    'TEXT'),
    ('auto_review_confidence', 'REAL'),
    ('auto_review_reason',     'TEXT'),
    ('auto_review_at',         'TEXT'),
]


def ensure_schema(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(tiktok_ads)")}
    for col, typ in NEW_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE tiktok_ads ADD COLUMN {col} {typ}")
            print(f"  + added column {col} {typ}")
    conn.commit()


def fetch_handle_context(conn: sqlite3.Connection, handle: str) -> dict | None:
    """Pull everything we know about a handle into one dict, ready for prompt."""
    rows = conn.execute("""
        SELECT advertiser_id, advertiser_disclosed_name, match_type,
               matched_candidate, matched_party, matched_district,
               ad_id, first_shown, last_shown, ad_status, status_statement,
               reach_raw, transcript, ad_url, ad_funded_by
        FROM tiktok_ads
        WHERE LOWER(advertiser_disclosed_name) = LOWER(?)
        ORDER BY first_shown DESC
    """, (handle,)).fetchall()
    if not rows:
        return None
    transcripts = [r[12] for r in rows if r[12]]
    return {
        'handle':            handle,
        'advertiser_id':     rows[0][0],
        'current_match_type': rows[0][2],
        'matched_candidate': rows[0][3],
        'matched_party':     rows[0][4],
        'matched_district':  rows[0][5],
        'n_ads':             len(rows),
        'ad_funded_by':      rows[0][14],
        'first_ad':          rows[-1][6],
        'last_ad':           rows[0][6],
        'first_shown':       rows[-1][7],
        'last_shown':        rows[0][8],
        'reach_buckets':     sorted({r[11] for r in rows if r[11]}),
        'sample_transcripts': transcripts[:3],
        'has_violation':     any('removed' in (r[10] or '').lower() or 'violation' in (r[10] or '').lower() for r in rows),
        'ad_urls_sample':    [r[13] for r in rows[:3]],
    }


def build_prompt(ctx: dict) -> str:
    """Construct the Claude prompt. Includes prompt-injection protection
    on transcript content (strip lines that try to spoof our output format)."""
    INJECTION_PREFIXES = ('verdict:', 'confidence:', 'reason:')
    safe_transcripts = []
    for t in ctx['sample_transcripts']:
        if not t:
            continue
        cleaned = '\n'.join(
            line for line in t[:1500].splitlines()
            if not line.strip().lower().startswith(INJECTION_PREFIXES)
        )
        safe_transcripts.append(cleaned)

    transcript_block = (
        '\n---\n'.join(safe_transcripts)
        if safe_transcripts
        else '(no transcripts available — judge from handle pattern + reach)'
    )

    candidate_str = ctx['matched_candidate'] or '(none)'
    party_str     = ctx['matched_party']     or '(none)'
    district_str  = ctx['matched_district']  or '(none)'

    return f"""You are reviewing a classification decision for a TikTok account that ran ads in Cyprus during the run-up to the 2026 parliamentary elections.

TikTok bans paid political ads globally, so anything classified as political-content is treated as enforcement-monitoring data.

Account being reviewed:
  Handle              : @{ctx['handle']}
  Currently classified: {ctx['current_match_type']}
    matched_candidate : {candidate_str}
    matched_party     : {party_str}
    matched_district  : {district_str}
  Ad volume           : {ctx['n_ads']} ads ({ctx['first_shown']} to {ctx['last_shown']})
  Reach buckets seen  : {', '.join(ctx['reach_buckets']) or '(none)'}
  Has TikTok-removed-for-violation ad: {'YES' if ctx['has_violation'] else 'no'}

Sample ad transcripts (oldest → newest, may contain Greek):
{transcript_block}

Pick the most accurate classification from this exact list:
  candidate       — an actual parliamentary candidate from one of the 2026 party lists
  supporter       — a private individual openly campaigning FOR a specific candidate or party
  party_account   — official party page / movement / youth wing
  commentator     — political commentary / opinion / news analysis (not a candidate themselves)
  news_outlet     — established news organization (newspapers, TV channels, websites)
  fp_business     — a commercial business that matched our keywords by coincidence
  fp_personal     — a personal account (lifestyle, religious, family etc.) that matched by coincidence
  unclear         — genuinely ambiguous, even with the evidence above

Respond in EXACTLY this 3-line format, nothing else:
VERDICT: <one word from the list above>
CONFIDENCE: <number 0.0 to 1.0>
REASON: <one short sentence, max 25 words, in English>"""


def call_claude(client, prompt: str) -> tuple[str, float, str]:
    """Returns (verdict, confidence, reason). Defaults to ('unclear', 0.0,
    error_msg) on any failure so the column is never null."""
    try:
        msg = client.messages.create(
            model='claude-haiku-4-5',
            max_tokens=120,
            messages=[{'role': 'user', 'content': prompt}],
        )
        reply = msg.content[0].text.strip()
        verdict_line    = next((l for l in reply.splitlines() if l.startswith('VERDICT:')),    '')
        confidence_line = next((l for l in reply.splitlines() if l.startswith('CONFIDENCE:')), '')
        reason_line     = next((l for l in reply.splitlines() if l.startswith('REASON:')),     '')
        verdict = verdict_line.replace('VERDICT:', '').strip().lower()
        try:
            confidence = float(confidence_line.replace('CONFIDENCE:', '').strip())
        except (ValueError, AttributeError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        reason = reason_line.replace('REASON:', '').strip()
        if verdict not in VALID_VERDICTS:
            verdict = 'unclear'
            reason = f'(invalid verdict {verdict!r} from model) ' + reason
        return verdict, confidence, reason
    except Exception as e:
        return 'unclear', 0.0, f'(API error: {type(e).__name__}: {str(e)[:120]})'


def save_verdict(conn: sqlite3.Connection, handle: str,
                  verdict: str, confidence: float, reason: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    n = conn.execute("""
        UPDATE tiktok_ads
           SET auto_review_verdict    = ?,
               auto_review_confidence = ?,
               auto_review_reason     = ?,
               auto_review_at         = ?
         WHERE LOWER(advertiser_disclosed_name) = LOWER(?)
    """, (verdict, confidence, reason, now, handle)).rowcount
    conn.commit()
    return n


def select_handles(conn: sqlite3.Connection, bucket: str, limit: int | None) -> list[str]:
    """Pull a list of handles to review based on which bucket the user asked for."""
    if bucket == 'recent_promotions':
        # promoted into a political tier in last 14 days
        sql = """
          SELECT DISTINCT advertiser_disclosed_name
          FROM tiktok_ads
          WHERE match_type NOT IN ('content_keyword')
            AND match_type NOT LIKE 'likely_false_positive%'
            AND advertiser_disclosed_name IS NOT NULL
            AND advertiser_disclosed_name != ''
            AND date(last_shown) >= date('now', '-14 days')
            AND auto_review_at IS NULL
        """
    elif bucket == 'lower_confidence':
        # commentator/supporter/etc. with at least one ad >=10K reach
        sql = """
          SELECT DISTINCT advertiser_disclosed_name
          FROM tiktok_ads
          WHERE match_type IN (
                  'commentator', 'podcast', 'satirist', 'news_outlet',
                  'politician_non_candidate', 'party_supporter', 'party_account'
              )
            AND advertiser_disclosed_name IS NOT NULL
            AND times_shown_upper_bound >= 10000
            AND auto_review_at IS NULL
        """
    elif bucket == 'all_unreviewed':
        sql = """
          SELECT DISTINCT advertiser_disclosed_name
          FROM tiktok_ads
          WHERE match_type NOT IN ('content_keyword')
            AND match_type NOT LIKE 'likely_false_positive%'
            AND advertiser_disclosed_name IS NOT NULL
            AND advertiser_disclosed_name != ''
            AND auto_review_at IS NULL
        """
    else:
        raise ValueError(f"unknown bucket: {bucket}")
    rows = [r[0] for r in conn.execute(sql).fetchall()]
    if limit:
        rows = rows[:limit]
    return rows


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                formatter_class=argparse.RawTextHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--handle',  help='Review a single handle')
    g.add_argument('--bucket',  choices=('recent_promotions', 'lower_confidence', 'all_unreviewed'),
                   help='Review every handle in this bucket')
    p.add_argument('--limit',   type=int, default=None,
                   help='Cap batch size (useful for testing)')
    p.add_argument('--sleep',   type=float, default=0.3,
                   help='Seconds between API calls (default 0.3)')
    p.add_argument('--dry-run', action='store_true',
                   help='Print verdict but don\'t write to DB')
    args = p.parse_args()

    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        sys.exit("ERROR: ANTHROPIC_API_KEY missing from .env")
    try:
        import anthropic
    except ImportError:
        sys.exit("ERROR: pip install anthropic")
    client = anthropic.Anthropic(api_key=api_key)

    conn = sqlite3.connect(DB)
    ensure_schema(conn)

    handles = [args.handle] if args.handle else select_handles(conn, args.bucket, args.limit)
    if not handles:
        print("  No candidates to review.")
        sys.exit(0)
    print(f"  Reviewing {len(handles)} handle(s) with Claude Haiku…\n")

    for i, h in enumerate(handles, 1):
        ctx = fetch_handle_context(conn, h)
        if ctx is None:
            print(f"  [{i}/{len(handles)}] @{h}  — not in DB, skipping")
            continue
        prompt = build_prompt(ctx)
        verdict, confidence, reason = call_claude(client, prompt)
        # Flag disagreement: claude's verdict vs our current tier
        tier = ctx['current_match_type']
        AGREE = {
            'candidate':      {'manual_resume'},
            'supporter':      {'party_supporter'},
            'commentator':    {'commentator', 'satirist'},
            'party_account':  {'party_account', 'party_coordinator', 'political_movement'},
            'news_outlet':    {'news_outlet', 'podcast'},
            'fp_business':    {'likely_false_positive_business'},
            'fp_personal':    {'likely_false_positive_personal'},
            'unclear':        set(),   # never agrees automatically
        }
        agrees = tier in AGREE.get(verdict, set())
        flag = '✓' if agrees else '⚠'
        print(f"  [{i}/{len(handles)}] {flag} @{h:30s}  "
              f"tier={tier:25s} → claude={verdict:14s}  ({confidence:.2f})  "
              f"{reason[:80]}")
        if not args.dry_run:
            n = save_verdict(conn, h, verdict, confidence, reason)
            if n == 0:
                print(f"      WARN: 0 rows updated (handle not found?)")
        time.sleep(args.sleep)

    conn.close()
    print(f"\n  ✓ done. Dashboard's 🔍 Review queue will surface disagreements.")


if __name__ == '__main__':
    main()
