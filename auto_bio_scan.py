"""Auto bio-scan: visit every readable-handle profile in our DB and check
   if their bio identifies them as a parliamentary candidate.

   Strategy:
     1. For each handle, open https://www.tiktok.com/@<handle>
     2. Extract page text (bio is visible without login)
     3. Pattern-match for candidacy indicators in Greek/English:
        - 'Υποψήφι' (candidate)
        - 'Βουλευτής' (MP)
        - Party names + variants (Ε.ΛΑ.Μ, Δ.ΗΣΥ, etc.)
        - 'Parliamentary Candidate', 'MP'
     4. Save bio + matched patterns + suggested party to cache JSON
     5. Skip already-confirmed (manual_resume) handles

   Resumable: cache lives at tiktok_bio_scan.json.
"""
import sys, asyncio, sqlite3, json, os, re
sys.stdout.reconfigure(encoding='utf-8')

DB = r'C:\Users\milit\meta_pipeline_data\politician_ads.db'
BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, 'tiktok_bio_scan.json')

# Pattern library
CANDIDATE_PATTERNS = [
    r'Υποψήφι[αο][υςνmotor]?\s*Βουλευτ',     # Υποψήφια Βουλευτής, Υποψήφιος Βουλευτής
    r'Υποψήφι[αο][υςν]?\b',                    # bare "Υποψήφια/ος"
    r'\bΒουλευτή[ςς]?\b',                      # Βουλευτής
    r'parliamentary\s+candidate',
    r'\bMP\b',
    r'candidate\s+for',
    r'υποψηφιότητα',                            # candidacy
    r'εκλογ[έεή][ςν]\s+2026',
    r'βουλευτικ[έεή]ς\s+εκλογ',
]
# Party detection — including dotted/spaced variants
PARTIES = {
    'ΑΚΕΛ':            [r'ΑΚΕΛ', r'Α\.Κ\.Ε\.Λ', r'\bAKEL\b'],
    'ΔΗΣΥ':            [r'ΔΗΣΥ', r'Δ\.ΗΣΥ', r'Δημοκρατικός\s+Συναγερμός', r'\bDISY\b', r'ΔΗ\.ΣΥ'],
    'ΔΗΚΟ':            [r'ΔΗΚΟ', r'Δ\.Η\.Κ\.Ο', r'ΔΗ\.ΚΟ', r'Δημοκρατικ[όο]\s+Κόμμα', r'\bDIKO\b'],
    'ΕΔΕΚ':            [r'ΕΔΕΚ', r'Ε\.Δ\.Ε\.Κ', r'\bEDEK\b'],
    'ΕΛΑΜ':            [r'ΕΛΑΜ', r'Ε\.ΛΑ\.Μ', r'Ε\.Λ\.Α\.Μ', r'Εθνικ[όο]\s+Λαϊκ[όο]\s+Μέτωπ', r'\bELAM\b'],
    'ΔΗΠΑ':            [r'ΔΗΠΑ', r'Δ\.Η\.Π\.Α', r'\bDIPA\b'],
    'ΒΟΛΤ':            [r'ΒΟΛΤ', r'\bVolt\b\s+Cyprus', r'\bVOLT\b\s+Κύπρου'],
    'ΑΛΜΑ':            [r'\bΑΛΜΑ\b'],
    'ΑΜΔΗ':            [r'ΑΜΔΗ', r'Άμεση[ς]?\s+Δημοκρατί'],
    'ΟΙΚΟΛΟΓΟΙ':       [r'ΟΙΚΟΛΟΓΟΙ', r'Οικολόγο[ιί]\b'],
    'ΣΗΚΟΥ ΠΑΝΩ':      [r'ΣΗΚΟΥ\s+ΠΑΝΩ', r'Σήκου\s+Πάνω'],
    'ΕΝΕΡΓΟΙ ΠΟΛΙΤΕΣ': [r'ΕΝΕΡΓΟΙ\s+ΠΟΛΙΤΕΣ', r'Ενεργοί\s+Πολίτες', r'Ενεργοι\s+Πολιτες'],
    'ΛΑΚΕΔΑΙΜΟΝΙΟΙ':   [r'ΛΑΚΕΔΑΙΜΟΝΙΟΙ', r'Λακεδαιμόνιοι'],
    'ΠΡΑΣΙΝΟΙ':        [r'ΠΡΑΣΙΝΟΙ', r'Πράσινοι'],
    'ΔΕΚ':             [r'\bΔΕΚ\b'],
    'ΑΓΡΟΝΟΜΟΣ':       [r'ΑΓΡΟΝΟΜΟΣ'],
    'ΛΑΕ':             [r'\bΛΑΕ\b'],
}
DISTRICTS = ['Λευκωσία','Λεμεσός','Λάρνακα','Πάφος','Αμμόχωστος','Κερύνεια',
             'Lefkosia','Lemesos','Larnaca','Paphos','Pafos','Famagusta','Ammochostos','Keryneia','Kyrenia','Nicosia']

def load_cache():
    if os.path.exists(CACHE):
        try:
            with open(CACHE, encoding='utf-8') as f: return json.load(f)
        except Exception: pass
    return {}
def save_cache(d):
    with open(CACHE, 'w', encoding='utf-8') as f:
        json.dump(d, f, indent=2, ensure_ascii=False)

# Get target handles
c = sqlite3.connect(DB)
already_confirmed = {r[0] for r in c.execute(
    "SELECT DISTINCT advertiser_disclosed_name FROM tiktok_ads WHERE match_type='manual_resume'")}
already_fp = {r[0] for r in c.execute(
    "SELECT DISTINCT advertiser_disclosed_name FROM tiktok_ads WHERE match_type LIKE 'likely_false_positive%'")}
skip = already_confirmed | already_fp

handles = [r[0] for r in c.execute("""
    SELECT DISTINCT advertiser_disclosed_name FROM tiktok_ads
    WHERE advertiser_disclosed_name IS NOT NULL
      AND advertiser_disclosed_name NOT GLOB '[0-9]*'
""")]
handles = [h for h in handles if h not in skip]
print(f"  Scanning {len(handles)} unverified handles (skipping {len(skip)} confirmed/FP)\n")

cache = load_cache()

async def scan_one(ctx, handle):
    if handle in cache and cache[handle].get('done'):
        return cache[handle]
    url = f"https://www.tiktok.com/@{handle}"
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until='networkidle', timeout=20000)
        await page.wait_for_timeout(2200)
        body = await page.inner_text('body')
        # Pull just the area near the profile (top 600 chars of body usually has bio)
        bio_region = body[:1500]
        # Detect candidacy patterns
        cand_hits = []
        for pat in CANDIDATE_PATTERNS:
            if re.search(pat, bio_region, re.IGNORECASE):
                cand_hits.append(pat)
        # Detect party
        party_hits = []
        for pname, pats in PARTIES.items():
            for pat in pats:
                if re.search(pat, bio_region):
                    party_hits.append(pname); break
        # District
        district_hits = [d for d in DISTRICTS if d in bio_region]
        result = {
            'handle': handle,
            'bio_excerpt': bio_region[:500].strip(),
            'candidacy_signal': bool(cand_hits),
            'cand_patterns': cand_hits,
            'parties': party_hits,
            'districts': district_hits,
            'done': True,
        }
    except Exception as e:
        result = {'handle': handle, 'error': str(e), 'done': False}
    finally:
        await page.close()
    return result

async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={'width':1366,'height':800})
        for i, h in enumerate(handles, 1):
            res = await scan_one(ctx, h)
            cache[h] = res
            if i % 10 == 0:
                save_cache(cache)
                print(f"  [{i}/{len(handles)}] cache checkpoint")
            if res.get('candidacy_signal') or res.get('parties'):
                p_str = '/'.join(res.get('parties') or []) or '?'
                d_str = '/'.join(res.get('districts') or []) or '?'
                print(f"  🎯 @{h:<30}  candidacy={res.get('candidacy_signal')}  party={p_str}  district={d_str}")
        save_cache(cache)
        await browser.close()

    # Final report
    candidates = [v for v in cache.values() if isinstance(v, dict) and v.get('candidacy_signal')]
    print(f"\n=== FOUND {len(candidates)} profiles with candidacy signal ===")
    for v in sorted(candidates, key=lambda x: x['handle']):
        p = '/'.join(v.get('parties') or []) or '?'
        d = '/'.join(v.get('districts') or []) or '?'
        bio = (v.get('bio_excerpt') or '').replace('\n', ' | ')[:200]
        print(f"\n  @{v['handle']}  party={p}  district={d}")
        print(f"     bio: {bio}")

asyncio.run(main())
