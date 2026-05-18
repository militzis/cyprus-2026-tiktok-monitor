"""Comprehensive content-keyword discovery on TikTok ad/query/ endpoint.
   Catches ads we missed via advertiser-name search — handles like
   @cyprus_voice_2026, party-level accounts, agency-run campaigns, etc.

   Pipeline:
   1. For each keyword, page through ad/query/ filtered to country_code=CY
   2. Collect unique (bid, ad_id) pairs, deduplicate against our existing 921 cache
   3. For each new advertiser bid, fetch ALL their CY ads via query_ads_for_advertiser
   4. Save to tiktok_ads with match_type='content_keyword' and the ad's
      content-search hit term in matched_party as a starting hint
   5. Persist a side-cache (content_keyword_discovery.json) for resumability
"""
import sys, os, time, json, sqlite3, importlib
from datetime import date, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding='utf-8')
import discover_tiktok_ads as t
importlib.reload(t)
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DB   = os.path.join(BASE, 'politician_ads.db')
SIDE_CACHE = os.path.join(BASE, 'content_keyword_discovery.json')

# ── Keyword universe ──────────────────────────────────────────────────────────
KEYWORDS = [
    # ── Party abbreviations (Greek) — all 19 parties in candidates.csv ──
    'ΑΚΕΛ', 'ΔΗΣΥ', 'ΔΗΚΟ', 'ΕΔΕΚ', 'ΕΛΑΜ', 'ΔΗΠΑ', 'ΒΟΛΤ', 'ΑΛΜΑ', 'ΑΜΔΗ',
    'ΟΙΚΟΛΟΓΟΙ', 'ΕΝΕΡΓΟΙ ΠΟΛΙΤΕΣ', 'ΣΗΚΟΥ ΠΑΝΩ', 'ΛΑΚΕΔΑΙΜΟΝΙΟΙ',
    'ΔΕΚ', 'ΑΓΡΟΝΟΜΟΣ', 'ΠΡΑΣΙΝΟΙ', 'ΛΑΕ', 'ΑΚΡΟ', 'ΔΑ',
    # ── Party full / official names (Greek) ──
    # NOTE: TikTok API limits search_term to 50 BYTES (UTF-8). Greek chars = 2 bytes
    # each, so phrases longer than ~25 Greek chars get rejected. Removed overflowing
    # variants — the shorter forms still match the same party-run accounts.
    'Ανορθωτικό Κόμμα',                       # ΑΚΕΛ (substring of full name, 16 chars)
    'Δημοκρατικός Συναγερμός',                # ΔΗΣΥ  (23c / 45b ✓)
    'Δημοκρατικό Κόμμα',                      # ΔΗΚΟ  (17c / 33b ✓)
    'Κίνημα Σοσιαλδημοκρατών',                # ΕΔΕΚ  (23c / 45b ✓)
    'Εθνικό Λαϊκό Μέτωπο',                    # ΕΛΑΜ  (19c / 37b ✓)
    'Δημοκρατική Παράταξη',                   # ΔΗΠΑ  (20c / 39b ✓)
    'Βολτ Κύπρος', 'Volt Cyprus',             # ΒΟΛΤ
    'Άλμα',                                    # ΑΛΜΑ
    'Άμεση Δημοκρατία',                       # ΑΜΔΗ  (16c / 31b ✓)
    'Κίνημα Οικολόγων',                       # ΟΙΚΟΛΟΓΟΙ  (16c / 31b ✓)
    'Συνεργασία Πολιτών',                     # ΟΙΚΟΛΟΓΟΙ subtitle  (18c / 35b ✓)
    'Ενεργοί Πολίτες',                        # ΕΝΕΡΓΟΙ ΠΟΛΙΤΕΣ  (15c / 29b ✓)
    'Κίνημα Αλληλεγγύης',                     # ΕΝΕΡΓΟΙ subtitle  (18c / 35b ✓)
    'Σήκου Πάνω',                             # ΣΗΚΟΥ ΠΑΝΩ
    'Λακεδαιμόνιοι',                          # ΛΑΚΕΔΑΙΜΟΝΙΟΙ
    'Δημοκρατική Ένωση',                      # ΔΕΚ (short form, 17c / 33b ✓)
    'Δημοκρατικό Εργατικό',                   # ΔΕΚ alt (20c / 39b ✓)
    'Αγρονόμος', 'Αγρονόμοι',                 # ΑΓΡΟΝΟΜΟΣ
    'Αγροτικό Κόμμα',                         # ΑΓΡΟΝΟΜΟΣ alt
    'Κίνημα Πρασίνων', 'Πράσινοι',            # ΠΡΑΣΙΝΟΙ
    'Πράσινο Κίνημα',                         # ΠΡΑΣΙΝΟΙ alt
    'Λαϊκή Άνοιξη',                           # ΛΑΕ (best-guess expansion)
    # ── Latin party abbreviations ──
    'AKEL', 'DISY', 'DIKO', 'EDEK', 'ELAM', 'DIPA', 'VOLT', 'AMDI', 'ALMA',
    'DEK', 'LAE', 'OIKOLOGOI', 'SIKOU PANO',
    # ── Election terminology — Greek ──
    'εκλογές', 'βουλευτικές', 'βουλευτικές εκλογές 2026',
    'υποψήφιος', 'υποψήφια', 'υποψηφιότητα', 'υποψήφιοι',
    'ψήφος', 'ψηφίζω', 'ψηφίστε', 'βουλή', 'βουλευτής', 'κοινοβούλιο',
    'πολιτική', 'πολιτικός', 'κόμμα', 'κίνημα', 'παράταξη',
    # Ballot-mark language (very Cyprus-specific) — NEW
    'σταυρός προτίμησης', 'σταυρό', 'σταυρώστε', 'βάλτε σταυρό',
    'ψηφοδέλτιο', 'ψηφοφορία',
    # ── Election terminology — Latin (for ads in English / mixed) ──
    'ekloges', 'vouleftikes', 'ypopsifios', 'voules',
    'vote cyprus', 'cyprus 2026', 'cyprus elections',
    'kypros 2026', 'kypros ekloges',
    # ── Districts (Greek + Latin) ──
    'Λευκωσία', 'Λεμεσός', 'Λάρνακα', 'Πάφος', 'Αμμόχωστος', 'Κερύνεια',
    'lefkosia', 'lemesos', 'larnaca', 'pafos', 'paphos',
    'ammochostos', 'famagusta', 'keryneia', 'kyrenia', 'nicosia',
    # ── Cyprus-issue vocabulary (high-precision political content) — NEW ──
    'Κυπριακό', 'λύση του Κυπριακού', 'κατοχή', 'κατεχόμενα',
    'πράσινη γραμμή', 'εποίκους', 'αγνοούμενοι',
    'Χριστοδουλίδης', 'κυβέρνηση Χριστοδουλίδη',
    'ρουσφέτι', 'διαφθορά', 'διαφάνεια',
    # ── ELAM-coded slogans (rare elsewhere) — NEW ──
    # (full triad "Ελληνισμός Ορθοδοξία Οικογένεια" is 60 bytes — over API limit,
    # so we search the most distinctive 2-word combos instead)
    'Ορθοδοξία Οικογένεια',                   # (20c / 39b ✓)
    'Ελληνισμός Ορθοδοξία',                   # (20c / 39b ✓)
    'απέλαση', 'μεταναστευτικό', 'λαθρομετανάστες',
    # ── Common political slogans / themes ──
    'αλλαγή', 'ανανέωση', 'μεταρρύθμιση', 'δημοκρατία', 'πατρίδα',
    'change cyprus', 'new cyprus',
]

# Existing bids in our DB so we know what's NEW
c = sqlite3.connect(DB)
KNOWN_BIDS = {row[0] for row in c.execute("SELECT DISTINCT advertiser_id FROM tiktok_ads")}
# Also load the older 921-discovery cache to widen "known"
disc_cache_path = os.path.join(BASE, 'tiktok_discovered.json')
if os.path.exists(disc_cache_path):
    with open(disc_cache_path, encoding='utf-8') as f:
        KNOWN_BIDS |= set((json.load(f).get('advertisers', {}) or {}).keys())
print(f"  starting with {len(KNOWN_BIDS)} known bids in DB+discovery cache\n")

# Resumable: load any progress from previous runs
side: dict = {'tried_keywords': [], 'found_ads': {}}
if os.path.exists(SIDE_CACHE):
    with open(SIDE_CACHE, encoding='utf-8') as f:
        side = json.load(f)
    print(f"  resuming: {len(side['tried_keywords'])} keywords already done, {len(side['found_ads'])} unique ads cached")

t.get_access_token()
tok = t.get_access_token()
SINCE = '20250901'
TODAY_MINUS_1 = (date.today() - timedelta(days=1)).strftime('%Y%m%d')


def search_ads_by_keyword(keyword: str, max_pages: int = 5) -> list[dict]:
    """Paginate through ad/query/ for one keyword, country_code=CY."""
    all_ads = []
    search_id = None
    for page in range(max_pages):
        body = {
            'filters': {
                'ad_published_date_range': {'min': SINCE, 'max': TODAY_MINUS_1},
                'country_code': 'CY',
            },
            'search_term': keyword,
            'max_count': 50,
        }
        if search_id:
            body['search_id'] = search_id
        fields = ','.join([
            'ad.id', 'ad.first_shown_date', 'ad.last_shown_date',
            'ad.status', 'ad.status_statement',
            'ad.videos', 'ad.image_urls', 'ad.reach',
            'advertiser.business_id', 'advertiser.business_name',
            'advertiser.paid_for_by',
            # NB: 'advertiser.country_code' is only valid on ad/detail/, not ad/query/ — leaving it out
        ])
        try:
            data = t._api_post(t.ADS_URL, {'fields': fields}, body)
        except t.RateLimitExceeded:
            print(f"      [429] persistent — stopping pagination on '{keyword}'")
            raise
        except Exception as e:
            print(f"      [error] {e}")
            raise   # surface to outer try so we don't mark the keyword as tried
        payload = data.get('data', {})
        ads = payload.get('ads', [])
        all_ads.extend(ads)
        if not payload.get('has_more') or not payload.get('search_id'):
            break
        search_id = payload['search_id']
        time.sleep(t.REQUEST_DELAY)
    return all_ads


# ── Sweep keywords ────────────────────────────────────────────────────────────
new_bids: dict[str, dict] = {}     # bid -> {bid, business_name, country_code, hit_terms}
total_ads = 0
for kw in KEYWORDS:
    if kw in side['tried_keywords']:
        continue
    print(f"  searching '{kw}' ...")
    try:
        ads = search_ads_by_keyword(kw)
    except t.RateLimitExceeded:
        print(f"    saving progress and exiting — try again in an hour")
        break
    except Exception as e:
        print(f"    skipping '{kw}' due to error: {e}")
        continue
    side['tried_keywords'].append(kw)
    if not ads:
        with open(SIDE_CACHE, 'w', encoding='utf-8') as f:
            json.dump(side, f, indent=2, ensure_ascii=False)
        time.sleep(t.REQUEST_DELAY)
        continue
    total_ads += len(ads)
    new_for_kw = 0
    for ad in ads:
        ad_obj = ad.get('ad', {}) or {}
        adv    = ad.get('advertiser', {}) or {}
        bid    = adv.get('business_id')
        if not bid: continue
        bid_str = str(bid)
        ad_id   = str(ad_obj.get('id') or '')
        if not ad_id: continue
        # Track all ads even if advertiser is known (might be ads we missed for known advertisers)
        side['found_ads'].setdefault(ad_id, {
            'ad_id': ad_id, 'bid': bid_str, 'business_name': adv.get('business_name'),
            'first_shown': ad_obj.get('first_shown_date'),
            'last_shown':  ad_obj.get('last_shown_date'),
            'hit_terms': [],
            'ad_payload': ad,
        })
        if kw not in side['found_ads'][ad_id]['hit_terms']:
            side['found_ads'][ad_id]['hit_terms'].append(kw)
        if bid_str not in KNOWN_BIDS and bid_str not in new_bids:
            new_bids[bid_str] = {
                'bid': bid_str,
                'business_name': adv.get('business_name'),
                'country_code': adv.get('country_code'),
                'hit_terms': [kw],
            }
            new_for_kw += 1
        elif bid_str in new_bids and kw not in new_bids[bid_str]['hit_terms']:
            new_bids[bid_str]['hit_terms'].append(kw)
    print(f"    → {len(ads)} ads ({new_for_kw} from NEW advertisers)")
    # Checkpoint after every keyword
    with open(SIDE_CACHE, 'w', encoding='utf-8') as f:
        json.dump(side, f, indent=2, ensure_ascii=False)
    time.sleep(t.REQUEST_DELAY)

print(f"\n  swept {len(side['tried_keywords'])}/{len(KEYWORDS)} keywords")
print(f"  unique ads cached: {len(side['found_ads'])}")
print(f"  NEW advertisers (not in our existing cache): {len(new_bids)}")

# ── For each new advertiser, save their ads to tiktok_ads ─────────────────────
# We already have the ad payloads in side['found_ads'] — just dedupe and save
# Group by bid, then upsert. Use match_type='content_keyword'.
from collections import defaultdict
ads_by_bid = defaultdict(list)
for ad_id, rec in side['found_ads'].items():
    ads_by_bid[rec['bid']].append(rec)

saved_total = 0
for bid_str, ad_records in ads_by_bid.items():
    if bid_str in KNOWN_BIDS:
        continue   # skip — we already track this advertiser
    rows = []
    handle_or_id = ad_records[0].get('business_name') or bid_str
    hit_terms_set = set()
    for rec in ad_records:
        hit_terms_set.update(rec.get('hit_terms', []))
    hit_terms_str = ','.join(sorted(hit_terms_set))[:200]
    for rec in ad_records:
        ad = rec['ad_payload']
        ad_obj = ad.get('ad', {}) or {}
        av_obj = ad.get('advertiser', {}) or {}
        reach_raw = (ad_obj.get('reach') or {}).get('unique_users_seen') or ''
        lb, ub = t.parse_reach(reach_raw)
        ad_id = str(ad_obj.get('id') or '')
        rows.append({
            'ad_id': ad_id,
            'advertiser_id': bid_str,
            'advertiser_disclosed_name': str(handle_or_id),
            'ad_funded_by': av_obj.get('paid_for_by'),
            'country_code': av_obj.get('country_code') or 'CY',
            'ad_url': f'https://library.tiktok.com/ads/detail/?ad_id={ad_id}',
            'first_shown': t._fmt_date(ad_obj.get('first_shown_date', '')),
            'last_shown':  t._fmt_date(ad_obj.get('last_shown_date', '')),
            'ad_status':   ad_obj.get('status'),
            'status_statement': ad_obj.get('status_statement'),
            'videos_json':     json.dumps(ad_obj.get('videos') or [], ensure_ascii=False),
            'image_urls_json': json.dumps(ad_obj.get('image_urls') or [], ensure_ascii=False),
            'reach_raw':   reach_raw,
            'times_shown_lower_bound': lb,
            'times_shown_upper_bound': ub,
            'targeting_json': None,
            'matched_candidate': '',
            'matched_party':    f'[content-keyword hits: {hit_terms_str}]',
            'matched_district': '',
            'match_type': 'content_keyword',
            'is_political': 1,
        })
    # Filter out any ads whose advertiser_id is already in a PROMOTED tier
    # (manual_resume, needs_profile_verification, likely_false_positive_*).
    # Otherwise the upsert would demote them back to content_keyword.
    cdb = sqlite3.connect(t.DB_PATH)
    protected = {r[0] for r in cdb.execute("""
        SELECT DISTINCT advertiser_id FROM tiktok_ads
        WHERE match_type IN ('manual_resume','needs_profile_verification')
           OR match_type LIKE 'likely_false_positive_%'
    """)}
    cdb.close()
    rows_safe = [r for r in rows if r['advertiser_id'] not in protected]
    n = t.upsert_rows(rows_safe)
    saved_total += n

print(f"\n  saved {saved_total} new content-keyword ads to tiktok_ads (skipped already-promoted advertisers)\n")

print(f"  Next: run transcription on the new videos (it will skip what's already cached).")
