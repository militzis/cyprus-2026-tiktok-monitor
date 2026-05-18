"""
classify_ads.py — DB-first ad classifier for Cyprus and Malta.
─────────────────────────────────────────────────────────────────────────────
Reads directly from politician_ads.db, classifies each ad as election-related
(YES / NO / UNCERTAIN) using Claude Haiku, and writes the result back to the DB.

Two new columns are added to politician_ads (migrated automatically):
    election_related  TEXT   — 'YES' | 'NO' | 'UNCERTAIN'
    ai_reason         TEXT   — one-sentence explanation from the AI

Only ads that are NULL in election_related are classified (safe to re-run).
Ads without any text use a keyword pre-check on candidate name + page name.

Usage:
    python classify_ads.py                   # classify all unclassified ads (CY + MT)
    python classify_ads.py --country CY      # Cyprus only
    python classify_ads.py --country MT      # Malta only
    python classify_ads.py --limit 200       # process at most 200 ads (useful for testing)
    python classify_ads.py --dry-run         # print what would be classified, don't write
    python classify_ads.py --reset-no        # re-classify ads previously marked NO
    python classify_ads.py --country MT --since 2025-10-01
"""

import os, sys, re, time, sqlite3, argparse, unicodedata
from datetime import datetime, timezone
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv(override=True)

BASE       = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(BASE, "politician_ads.db")
DB_PATH_MT = os.path.join(BASE, "politician_ads_mt.db")
BL_CY      = os.path.join(BASE, "page_blocklist.json")
BL_MT      = os.path.join(BASE, "page_blocklist_mt.json")


def load_blocklist(path: str) -> set:
    """Return set of blocked page IDs from a blocklist JSON file."""
    if not os.path.exists(path):
        return set()
    import json
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    pages = data.get("pages", data) if isinstance(data, dict) else {}
    return set(str(k) for k in pages.keys())

# ── Election keywords ─────────────────────────────────────────────────────────
# Greek/Cypriot terms
CY_KEYWORDS = [
    "εκλογ", "ψηφ", "βουλευτ", "υποψήφ", "κόμμα", "κομματ",
    "δησυ", "ακελ", "δηκο", "εδεκ", "ελαμ", "αμδη", "βολτ",
    "ψηφοδέλτ", "βουλή", "επαρχ", "συνδυασμ", "πρόγραμμα",
]
# Maltese/English terms
MT_KEYWORDS = [
    "vot", "elezzjon", "parlament", "kandidat", "partit",
    "elezzjonijiet", "kamra", "deputat", "kostitwenza",
    "labour", "nationalist", "pl", "pn",
    "vote", "election", "parliament", "candidate", "party",
    "campaign", "manifesto", "constituency",
]

COMMON_KEYWORDS = [
    "election", "vote", "parliament", "candidate", "party",
    "campaign", "manifesto",
]

# Sources that are always election-related by design (page matched by name search)
TRUSTED_SOURCES = {"greek", "latin", "mt", "verified"}

# Clearly-non-election page name fragments (fast skip before AI)
SKIP_FRAGMENTS = [
    "real estate", "dhalia", "remax", "re/max", "property",
    "restaurant", "café", "coffee", "hotel", "fitness", "gym",
    "music society", "band club", "feast", "festa", "soċjetà",
    "science centre", "esplora", "xjenza", "news", "media",
    "foundation", "fondazzjoni", "charity",
]


# ── DB helpers ────────────────────────────────────────────────────────────────

def ensure_columns(conn: sqlite3.Connection) -> None:
    """Add election_related and ai_reason columns if they don't exist."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(politician_ads)")}
    if "election_related" not in cols:
        conn.execute("ALTER TABLE politician_ads ADD COLUMN election_related TEXT")
        print("[db] Added column: election_related")
    if "ai_reason" not in cols:
        conn.execute("ALTER TABLE politician_ads ADD COLUMN ai_reason TEXT")
        print("[db] Added column: ai_reason")
    conn.commit()


def load_unclassified(conn: sqlite3.Connection, country: str | None,
                      since: str, limit: int | None,
                      reset_no: bool, blocklist: set | None = None) -> list[dict]:
    """Fetch ads that haven't been classified yet (or NO if reset_no)."""
    where_parts = ["ad_start_date >= ?"]
    params: list = [since]

    if country == "CY":
        where_parts.append("source IN ('greek','latin','page_id_cy','cy')")
    elif country == "MT":
        where_parts.append("source IN ('malta','page_id_mt','mt')")
    # else: all countries

    if reset_no:
        where_parts.append("(election_related IS NULL OR election_related = 'NO')")
    else:
        where_parts.append("election_related IS NULL")

    # Include removed ads — we still need to know if they were election-related
    # so they show up correctly in the "Removed by Meta" dashboard metric

    where_sql = " AND ".join(where_parts)
    sql = f"""
        SELECT ad_archive_id, politician_query, party, district,
               page_name, page_id, source, ad_text,
               ad_start_date, ad_stop_date
        FROM politician_ads
        WHERE {where_sql}
        ORDER BY ad_start_date DESC
    """
    if limit:
        sql += f" LIMIT {limit}"

    rows = conn.execute(sql, params).fetchall()
    cols = ["ad_archive_id", "politician_query", "party", "district",
            "page_name", "page_id", "source", "ad_text",
            "ad_start_date", "ad_stop_date"]
    ads = [dict(zip(cols, r)) for r in rows]
    if blocklist:
        before = len(ads)
        ads = [a for a in ads if str(a.get("page_id") or "") not in blocklist]
        skipped = before - len(ads)
        if skipped:
            print(f"  [blocklist] Skipped {skipped} ads from blocked pages")
    return ads


def write_result(conn: sqlite3.Connection, ad_id: str,
                 related: str, reason: str) -> None:
    conn.execute(
        "UPDATE politician_ads SET election_related=?, ai_reason=? WHERE ad_archive_id=?",
        (related, reason, ad_id)
    )


# ── Keyword pre-check ─────────────────────────────────────────────────────────

def _kw_match(keywords: list, text: str) -> bool:
    """Return True if any keyword appears as a whole word in text."""
    for kw in keywords:
        # Use word-boundary regex so 'party' doesn't match 'partyline'
        if re.search(r'(?<!\w)' + re.escape(kw) + r'(?!\w)', text):
            return True
    return False


def keyword_check(text: str, page_name: str, query: str, source: str) -> str | None:
    """
    Fast pre-screen before calling the AI.
    Returns 'YES', 'NO', or None (= needs AI).
    Keywords are matched as whole words only — 'party' won't match 'partyline'.
    """
    text_lower = text.lower()
    page_lower = page_name.lower()
    # Keywords checked only in ad text, NOT page name (page name checked separately)
    all_kw = CY_KEYWORDS + MT_KEYWORDS

    # Pages from name-search sources are already human-vetted as candidates
    if source in TRUSTED_SOURCES and query:
        # Still check for obvious false-positives (businesses leaked in)
        if any(frag in page_lower for frag in SKIP_FRAGMENTS):
            return None   # let AI decide
        # If we have text and it mentions a keyword (whole word), confident YES
        if text and _kw_match(all_kw, text_lower):
            return "YES"
        # No text — send to AI
        if not text:
            return None

    # Clearly irrelevant by page name
    if any(frag in page_lower for frag in SKIP_FRAGMENTS):
        return "NO"

    # Strong keyword hit in ad text only (not page name)
    if text and _kw_match(all_kw, text_lower):
        return "YES"

    return None  # Needs AI


# ── AI classification ─────────────────────────────────────────────────────────

def classify_with_ai(ad: dict, client) -> tuple[str, str]:
    """Call Claude Haiku to classify the ad. Returns (YES/NO/UNCERTAIN, reason)."""
    try:
        import anthropic
    except ImportError:
        return "UNCERTAIN", "anthropic package not installed"

    query  = ad.get("politician_query") or ""
    name   = query.split("|")[0].strip() if "|" in query else query.strip()
    party  = ad.get("party") or (query.split("|")[1].strip() if "|" in query else "")
    source = ad.get("source") or ""
    country_hint = "Cyprus" if source in ("greek", "latin", "page_id_cy") else "Malta"

    text = ad.get("ad_text") or ""
    page = ad.get("page_name") or ""

    # Sanitise ad_text to prevent prompt injection: strip any lines that start
    # with "RELATED:" or "REASON:" so an advertiser cannot spoof the expected
    # output format by embedding it in their ad copy.
    _INJECTION_PREFIXES = ("related:", "reason:")
    text_safe = "\n".join(
        line for line in text[:600].splitlines()
        if not line.strip().lower().startswith(_INJECTION_PREFIXES)
    ) if text else ""

    prompt = f"""You are analysing a Facebook ad from {country_hint} ({country_hint} parliamentary elections 2025-2026).
Determine if this ad is directly related to electoral campaigning or political candidates.

Candidate in our database : {name}
Party                     : {party}
Facebook page name        : {page}
Ad text                   : {text_safe if text_safe else "(no text — judge by page name and candidate name)"}

Answer ONLY in this exact format (two lines, no extra text):
RELATED: YES / NO / UNCERTAIN
REASON: (one sentence, max 15 words)"""

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        reply = msg.content[0].text.strip()
        related_line = next((l for l in reply.splitlines() if l.startswith("RELATED:")), "")
        reason_line  = next((l for l in reply.splitlines() if l.startswith("REASON:")),  "")
        related = related_line.replace("RELATED:", "").strip().upper()
        reason  = reason_line.replace("REASON:", "").strip()
        if related not in ("YES", "NO", "UNCERTAIN"):
            related = "UNCERTAIN"
        return related, reason
    except Exception as e:
        return "UNCERTAIN", f"AI error: {str(e)[:80]}"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Classify ads as election-related using Claude Haiku.")
    parser.add_argument("--country",   choices=["CY", "MT"], default=None,
                        help="Limit to CY or MT (default: both)")
    parser.add_argument("--since",     default="2025-10-01",
                        help="Earliest ad_start_date to classify (default: 2025-10-01)")
    parser.add_argument("--limit",     type=int, default=None,
                        help="Max ads to classify this run (useful for testing)")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Print what would be classified without writing to DB")
    parser.add_argument("--reset-no",  action="store_true",
                        help="Re-classify ads previously marked as NO")
    parser.add_argument("--sleep",     type=float, default=0.3,
                        help="Seconds between AI calls (default: 0.3)")
    parser.add_argument("--batch",     type=int,   default=50,
                        help="Commit to DB every N ads (default: 50)")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key and not args.dry_run:
        sys.exit("ERROR: ANTHROPIC_API_KEY not set in .env")

    # Lazy-import anthropic only when needed
    ai_client = None
    if not args.dry_run:
        try:
            import anthropic as _anthropic
            ai_client = _anthropic.Anthropic(api_key=api_key)
        except ImportError:
            sys.exit("ERROR: anthropic package not installed. Run: pip install anthropic")

    # Determine which DB(s) to process
    if args.country == "MT":
        db_targets = [("MT", DB_PATH_MT, BL_MT)]
    elif args.country == "CY":
        db_targets = [("CY", DB_PATH, BL_CY)]
    else:
        # No --country specified: process both CY and MT
        db_targets = [("CY", DB_PATH, BL_CY), ("MT", DB_PATH_MT, BL_MT)]

    grand_yes = grand_no = grand_unc = grand_kw = grand_total = 0

    for country_label, db_path, bl_path in db_targets:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        ensure_columns(conn)

        blocklist = load_blocklist(bl_path)
        # When processing both DBs, pass the specific country filter to load_unclassified
        country_filter = country_label if len(db_targets) > 1 else args.country
        ads = load_unclassified(conn, country_filter, args.since, args.limit,
                                args.reset_no, blocklist=blocklist)
        total = len(ads)

        print(f"\nAd classifier — {country_label}  |  since {args.since}  |  db: {os.path.basename(db_path)}")
        print(f"Ads to classify : {total:,}")
        if args.dry_run:
            print("DRY RUN — no changes will be written.\n")
        print("─" * 60)

        if total == 0:
            print("Nothing to classify. All ads already have election_related set.")
            conn.close()
            continue

        # Cost estimate (Claude Haiku ~$0.25/1M input tokens, ~80 tokens/call avg)
        estimated_cost = total * 80 * 0.25 / 1_000_000
        print(f"Estimated AI cost : ~${estimated_cost:.2f}  ({total} × ~80 tokens @ $0.25/1M)")
        print("─" * 60)

        yes_n = no_n = unc_n = kw_skip_n = 0
        pending: list[tuple[str, str, str]] = []   # (ad_id, related, reason) batch

        for i, ad in enumerate(ads, 1):
            ad_id     = ad["ad_archive_id"]
            page_name = ad.get("page_name") or ""
            query     = ad.get("politician_query") or ""
            cand      = query.split("|")[0][:30] if query else page_name[:30]
            text      = ad.get("ad_text") or ""
            source    = ad.get("source") or ""

            # 1. Keyword pre-check
            kw_result = keyword_check(text, page_name, query, source)

            if kw_result == "YES":
                related, reason = "YES", "Keyword pre-check: election terms found"
                kw_skip_n += 1
            elif kw_result == "NO":
                related, reason = "NO", "Keyword pre-check: no election terms, flagged page"
                kw_skip_n += 1
            else:
                # 2. AI classification
                if args.dry_run:
                    related, reason = "UNCERTAIN", "(dry-run, AI not called)"
                else:
                    related, reason = classify_with_ai(ad, ai_client)
                    time.sleep(args.sleep)

            # Tally
            if related == "YES":    yes_n += 1
            elif related == "NO":   no_n  += 1
            else:                   unc_n += 1

            ai_or_kw = "kw" if kw_result else "AI"
            print(f"[{i:>5}/{total}] {related:<9}  ({ai_or_kw})  {cand}")

            if not args.dry_run:
                pending.append((ad_id, related, reason))

                # Batch commit
                if len(pending) >= args.batch:
                    for pid, prel, prsn in pending:
                        write_result(conn, pid, prel, prsn)
                    conn.commit()
                    print(f"  ── committed {len(pending)} records ──")
                    pending.clear()

        # Final flush
        if pending and not args.dry_run:
            for pid, prel, prsn in pending:
                write_result(conn, pid, prel, prsn)
            conn.commit()
            print(f"  ── committed {len(pending)} records ──")

        conn.close()

        print(f"\n{'─'*60}")
        print(f"  Country         : {country_label}")
        print(f"  Total processed : {total:,}")
        print(f"  ✅ YES           : {yes_n:,}")
        print(f"  ❌ NO            : {no_n:,}")
        print(f"  ❓ UNCERTAIN     : {unc_n:,}")
        print(f"  ⚡ Keyword skip  : {kw_skip_n:,}  (no AI call needed)")
        if not args.dry_run:
            print(f"  Results written to: {db_path}")

        grand_yes   += yes_n
        grand_no    += no_n
        grand_unc   += unc_n
        grand_kw    += kw_skip_n
        grand_total += total

    if len(db_targets) > 1:
        print(f"\n{'═'*60}")
        print(f"  GRAND TOTAL  CY + MT")
        print(f"  Processed : {grand_total:,}  |  YES {grand_yes:,}  NO {grand_no:,}  UNC {grand_unc:,}")

    print(f"\nDone.")


if __name__ == "__main__":
    main()
