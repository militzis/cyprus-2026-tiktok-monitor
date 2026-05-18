"""Smarter handle→candidate matcher that catches what we've been missing:

   1. TRANSLITERATION VARIANTS — Greek letters with multiple Latin renderings:
      κ → k OR c     (Νικολάου → nikolaou OR nicolaou)
      χ → ch OR h    (Χαμπούλλας → chamboullas OR hamboullas)
      θ → th OR t
      φ → f OR ph
      υ → y OR u OR i

   2. SURNAME + INITIAL pattern (e.g. paliosn = Palios + N for Nikos)

   3. Bare-surname handle when surname is ≥6 chars + distinctive
      (only 1 candidate has that surname in CSV)

   Runs over every readable handle in the DB. No API calls. Instant.
"""
import sys, csv, sqlite3, os, re
from collections import defaultdict
sys.stdout.reconfigure(encoding='utf-8')

DB   = r'C:\Users\milit\meta_pipeline_data\politician_ads.db'
BASE = os.path.dirname(os.path.abspath(__file__))

# Greek → Latin: multiple variants per letter
TD = {'ου':'ou','αυ':'av','ευ':'ev','οι':'oi','αι':'ai','ει':'ei','γγ':'ng','γκ':'gk','μπ':'mp','ντ':'nt','τζ':'tz','ού':'ou','αύ':'av','εύ':'ev','οί':'oi','αί':'ai','εί':'ei'}
TC = {'α':'a','β':'v','γ':'g','δ':'d','ε':'e','ζ':'z','η':'i','θ':'th','ι':'i','κ':'k','λ':'l','μ':'m','ν':'n','ξ':'x','ο':'o','π':'p','ρ':'r','σ':'s','ς':'s','τ':'t','υ':'y','φ':'f','χ':'ch','ψ':'ps','ω':'o','ά':'a','έ':'e','ή':'i','ί':'i','ό':'o','ύ':'y','ώ':'o','ϊ':'i','ϋ':'y','ΐ':'i','ΰ':'y'}
# Alternate mappings for variant generation
VARIANTS = {
    'k':  ['k', 'c'],   # κ
    'ch': ['ch', 'h'],  # χ
    'th': ['th', 't'],  # θ
    'f':  ['f', 'ph'],  # φ
    'y':  ['y', 'u', 'i'],  # υ
}

def translit(s):
    s = s.lower(); out = ''; i = 0
    while i < len(s):
        two = s[i:i+2]
        if two in TD: out += TD[two]; i += 2
        else: out += TC.get(s[i], s[i]); i += 1
    return out

def variants_of(name_lat):
    """Generate all transliteration variants of a Latin string."""
    # Order matters: try multi-char substitutions first
    results = {name_lat}
    for src, alts in VARIANTS.items():
        new = set()
        for r in results:
            for alt in alts:
                if src in r:
                    new.add(r.replace(src, alt))
                else:
                    new.add(r)
        results = new
    return results

# ── Load candidates with variants ────────────────────────────────────────────
candidates = []
with open(os.path.join(BASE, 'candidates.csv'), encoding='utf-8') as f:
    for row in csv.DictReader(f):
        n = row.get('name','').strip()
        if not n: continue
        parts = n.split()
        if not parts: continue
        last  = translit(parts[0]).lower()
        first = translit(parts[-1]).lower() if len(parts) >= 2 else ''
        if not first or not last: continue
        last_variants  = variants_of(last)
        first_variants = variants_of(first)
        candidates.append({
            'name': n, 'party': row.get('party',''), 'district': row.get('district',''),
            'first': first, 'last': last,
            'last_variants': last_variants,
            'first_variants': first_variants,
            'concat_variants_lf': {l + f for l in last_variants for f in first_variants},
            'concat_variants_fl': {f + l for l in last_variants for f in first_variants},
            'first_initials': {f[0] for f in first_variants if f},
        })

# Distinctive-surname index: surname (≥6 chars) shared by ≤1 candidate
last_counts = defaultdict(int)
for c in candidates:
    for l in c['last_variants']:
        if len(l) >= 6:
            last_counts[l] += 1

print(f"  Loaded {len(candidates)} candidates with variant transliterations")

# ── Pull every readable handle in DB ─────────────────────────────────────────
c = sqlite3.connect(DB)
already_known = {r[0] for r in c.execute(
    "SELECT DISTINCT advertiser_id FROM tiktok_ads WHERE match_type='manual_resume'")}
handles = list(c.execute("""
    SELECT advertiser_id, advertiser_disclosed_name, COUNT(*) AS ads, match_type
    FROM tiktok_ads
    WHERE advertiser_disclosed_name IS NOT NULL
      AND advertiser_disclosed_name NOT GLOB '[0-9]*'
    GROUP BY advertiser_id
"""))
print(f"  Scanning {len(handles)} readable handles "
      f"(skipping {len(already_known)} already-confirmed)\n")

# ── Match ────────────────────────────────────────────────────────────────────
new_strong  = []   # both names match (one of many variants)
new_initial = []   # surname + first-initial (e.g. paliosn)
new_bare    = []   # bare distinctive surname

for bid, handle, ads, mt in handles:
    if bid in already_known: continue
    h = handle.lower()
    h_concat = re.sub(r'[^a-z0-9]', '', h)
    h_tokens = set(re.split(r'[^a-z0-9]+', h))

    for cand in candidates:
        if len(cand['last']) < 4 or len(cand['first']) < 4: continue

        # 1. STRONG: full-name concat in either order, any transliteration variant
        if (cand['concat_variants_lf'] & {h_concat} or
            cand['concat_variants_fl'] & {h_concat} or
            any(v in h_concat for v in cand['concat_variants_lf'] | cand['concat_variants_fl'])):
            new_strong.append((bid, handle, ads, mt, cand, 'concat-variant'))
            continue

        # 2. Token-match: both last AND first appear separately
        if (cand['last_variants'] & h_tokens) and (cand['first_variants'] & h_tokens):
            new_strong.append((bid, handle, ads, mt, cand, 'both-tokens'))
            continue

        # 3. SURNAME + INITIAL pattern (paliosn = palios + n)
        for lv in cand['last_variants']:
            if len(lv) >= 5 and h_concat.startswith(lv) and len(h_concat) - len(lv) <= 2:
                tail = h_concat[len(lv):]
                if tail and tail[0] in cand['first_initials']:
                    new_initial.append((bid, handle, ads, mt, cand, f"surname+initial '{tail}'"))
                    break

        # 4. BARE DISTINCTIVE SURNAME: handle == surname (≥6 chars), only one candidate has it
        for lv in cand['last_variants']:
            if len(lv) >= 6 and h_concat == lv and last_counts.get(lv, 99) <= 1:
                new_bare.append((bid, handle, ads, mt, cand, f"bare-surname '{lv}'"))
                break

# Dedupe — same bid can match multiple candidates (homonyms); show all
def show(title, group):
    if not group: return
    print(f"## {title} ({len(group)} hits)\n")
    seen = set()
    for bid, handle, ads, mt, cand, why in group:
        if (bid, cand['name']) in seen: continue
        seen.add((bid, cand['name']))
        print(f"  {ads:>3} ads  @{handle:<28} → {cand['name']:<24} ({cand['party']:<14} — {cand['district']:<12})  [{why}]")
    print()

show("STRONG (full-name match via variant transliteration)", new_strong)
show("SURNAME + INITIAL (e.g. paliosn = Palios + N)", new_initial)
show("BARE DISTINCTIVE SURNAME (≥6 chars, only 1 candidate)", new_bare)
