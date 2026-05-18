"""
discover_tiktok_ads.py — Discover Cyprus political ads via the TikTok
Commercial Content API.

Strategy (mirrors discover_google_ads.py, no Playwright needed):
  1. OAuth client-credentials → bearer token (cached 2h on disk).
  2. For each candidate last-name / party / generic political keyword,
     call query_advertisers → filter results where country_code == 'CY'.
  3. For each CY advertiser, call query_ads filtered by their business_id
     (paginated). Date range starts at DEFAULT_START.
  4. Match each advertiser against candidates.csv.
  5. Upsert rows into tiktok_ads table in politician_ads.db.

Note: TikTok bans paid political ads globally. Any candidate/party hit
in this library is, by definition, a likely policy violation —
treat the output as enforcement-monitoring data, not transparency data.

Setup:
    pip install requests python-dotenv
    # Add to .env:
    TIKTOK_CLIENT_KEY=...
    TIKTOK_CLIENT_SECRET=...

Usage:
    python discover_tiktok_ads.py              # incremental
    python discover_tiktok_ads.py --full       # re-fetch from DEFAULT_START
    python discover_tiktok_ads.py --dry-run    # no DB writes
    python discover_tiktok_ads.py --limit 5    # only first 5 search terms
"""

import os, sys, csv, json, re, sqlite3, argparse, unicodedata, time, random
from datetime import date, datetime, timedelta, timezone

try:
    import requests
except ImportError:
    sys.exit("ERROR: pip install requests")

from dotenv import load_dotenv
load_dotenv(override=True)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

BASE             = os.path.dirname(os.path.abspath(__file__))
# DB lives OUTSIDE OneDrive — OneDrive's sync-conflict handling silently rolls
# back local SQLite writes between sessions. Keep the DB in a non-synced folder
# so saves persist. Override with env var POLITICIAN_ADS_DB if you relocate it.
DB_PATH          = os.environ.get(
    'POLITICIAN_ADS_DB',
    r'C:\Users\milit\meta_pipeline_data\politician_ads.db',
)
CANDIDATES_FILE  = os.path.join(BASE, "candidates.csv")
FETCH_STATE_FILE = os.path.join(BASE, "fetch_state.json")
TOKEN_CACHE_FILE = os.path.join(BASE, "tiktok_token_cache.json")
DISCOVERED_CACHE = os.path.join(BASE, "tiktok_discovered.json")
DEFAULT_START    = "2025-09-01"
OVERLAP_DAYS     = 7

CLIENT_KEY    = os.environ.get("TIKTOK_CLIENT_KEY", "")
CLIENT_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET", "")

API_BASE   = "https://open.tiktokapis.com"
OAUTH_URL  = f"{API_BASE}/v2/oauth/token/"
ADV_URL    = f"{API_BASE}/v2/research/adlib/advertiser/query/"
ADS_URL    = f"{API_BASE}/v2/research/adlib/ad/query/"

REQUEST_DELAY    = 2.0   # seconds between API calls (was 1.0)
COOLDOWN_429     = 600   # seconds to sleep after a burst of 429s
COOLDOWN_TRIGGER = 3     # consecutive 429s to trigger a cooldown
MAX_COOLDOWNS    = 3     # after this many cooldowns w/o forward progress, bail out
BACKOFF_BASE_429 = 30    # initial 429 wait — bumped from 5s (TikTok's window is ~60s)
BACKOFF_MAX_429  = 240   # cap on per-call exponential backoff


# ── Shared helpers (kept inline to decouple from discover_google_ads) ─────────

TRANSLIT_DIGRAPHS = {
    'ου':'ou','αυ':'av','ευ':'ev','οι':'oi','αι':'ai','ει':'ei',
    'γγ':'ng','γκ':'gk','μπ':'mp','ντ':'nt','τζ':'tz',
    'ού':'ou','αύ':'av','εύ':'ev','οί':'oi','αί':'ai','εί':'ei',
}
TRANSLIT = {
    'α':'a','β':'v','γ':'g','δ':'d','ε':'e','ζ':'z','η':'i','θ':'th',
    'ι':'i','κ':'k','λ':'l','μ':'m','ν':'n','ξ':'x','ο':'o','π':'p',
    'ρ':'r','σ':'s','ς':'s','τ':'t','υ':'y','φ':'f','χ':'ch','ψ':'ps',
    'ω':'o','ά':'a','έ':'e','ή':'i','ί':'i','ό':'o','ύ':'y','ώ':'o',
    'ϊ':'i','ϋ':'y','ΐ':'i','ΰ':'y',
}

def translit(text: str) -> str:
    text = text.lower()
    out, i = "", 0
    while i < len(text):
        two = text[i:i+2]
        if two in TRANSLIT_DIGRAPHS:
            out += TRANSLIT_DIGRAPHS[two]; i += 2
        else:
            out += TRANSLIT.get(text[i], text[i]); i += 1
    return out

def name_to_latin(name: str) -> str:
    return " ".join(translit(p).capitalize() for p in name.strip().split())

def _norm(s: str) -> str:
    return ''.join(
        c for c in unicodedata.normalize('NFD', (s or '').lower())
        if unicodedata.category(c) != 'Mn'
    )

def _words(text: str) -> set:
    return set(re.split(r'[\s\-\_\.\,\(\)\/\&\+]+', text))


POLITICAL_ADVERTISER_KEYWORDS = [
    # Greek abbreviations
    'ακελ', 'δησυ', 'δηκο', 'δηπα', 'εδεκ', 'ελαμ', 'βολτ',
    'οικολογ', 'αλμα', 'αμδη', 'σηκου πανω', 'λακεδαιμον', 'αγρονομ',
    'πρασινοι', 'λαε', 'ακρο',
    # Latin abbreviations
    'akel', 'disy', 'diko', 'dipa', 'edek', 'elam', 'volt', 'amdi', 'alma',
    'oikologoi', 'sikou pano',
    # Full Greek party names (substring match — catches party-run accounts)
    'δημοκρατικός συναγερμός', 'δημοκρατικό κόμμα', 'δημοκρατική παράταξη',
    'εθνικό λαϊκό μέτωπο', 'ανορθωτικό κόμμα εργαζόμενου λαού',
    'κίνημα σοσιαλδημοκρατών', 'άμεση δημοκρατία', 'σήκου πάνω',
    'κίνημα οικολόγων', 'συνεργασία πολιτών',
    'ενεργοί πολίτες', 'κίνημα αλληλεγγύης',
    'βολτ κύπρος', 'volt cyprus',
    'συνεργασία δημοκρατικών δυνάμεων',
    'δημοκρατική ένωση κύπρου',
    'κίνημα πρασίνων',
    # Generic
    'κόμμα', 'κινημα', 'πολιτης τωρα',
]

# Short party abbreviations need an exact-token match, otherwise 'dipa' matches
# personal handles like 'dipak_tmng' / 'dipa9256'.
_STRICT_KEYWORDS = {
    # Latin (3-4 chars need strict token match)
    'akel','disy','diko','dipa','edek','elam','volt','alma','amdi',
    'dek','lae','akro','da',
    # Greek
    'ακελ','δησυ','δηκο','δηπα','εδεκ','ελαμ','βολτ','αλμα','αμδη',
    'δεκ','λαε','ακρο','δα',
}
_TOKEN_SPLIT = re.compile(r'[^a-zA-Z0-9Ͱ-Ͽ]+')

def advertiser_name_is_political(name: str) -> bool:
    if not name:
        return False
    n = name.lower()
    # Exact-token match for short party abbreviations
    tokens = set(_TOKEN_SPLIT.split(n))
    if tokens & _STRICT_KEYWORDS:
        return True
    # Substring match for longer keywords / multi-word phrases
    loose = [kw for kw in POLITICAL_ADVERTISER_KEYWORDS if kw not in _STRICT_KEYWORDS]
    return any(kw in n for kw in loose)


def load_candidate_index(path: str) -> list[dict]:
    with open(path, encoding='utf-8') as f:
        candidates = list(csv.DictReader(f))
    index = []
    for c in candidates:
        name     = c.get('name', '').strip()
        party    = c.get('party', '').strip()
        district = c.get('district', '').strip()
        if not name:
            continue
        greek_parts = [p for p in _norm(name).split() if len(p) >= 3]
        latin_parts = [p.lower() for p in name_to_latin(name).split() if len(p) >= 3]
        greek_parts = list(dict.fromkeys(greek_parts))
        latin_parts = list(dict.fromkeys(latin_parts))
        index.append({
            'name': name, 'party': party, 'district': district,
            'greek_parts': greek_parts, 'latin_parts': latin_parts,
        })
    return index

def match_advertiser(name: str, funded_by: str, index: list[dict]) -> dict | None:
    search = _norm(name) + ' ' + _norm(funded_by or '')
    words  = _words(search)
    best = None
    for c in index:
        for key, tag in (('latin_parts', 'latin'), ('greek_parts', 'greek')):
            parts = c[key]
            if not parts:
                continue
            hits = [p for p in parts if p in words]
            if len(hits) == len(parts):
                if best is None or len(hits) > best['score']:
                    best = {**c, 'match_type': tag, 'score': len(hits)}
    return best


# ── Fetch-state ───────────────────────────────────────────────────────────────

def load_fetch_since(key: str) -> str:
    if os.path.exists(FETCH_STATE_FILE):
        try:
            with open(FETCH_STATE_FILE, encoding='utf-8') as f:
                state = json.load(f)
            last_str = state.get(key)
            if last_str:
                last  = date.fromisoformat(last_str)
                since = max(date.fromisoformat(DEFAULT_START),
                            last - timedelta(days=OVERLAP_DAYS))
                return since.isoformat()
        except Exception as e:
            print(f"[state] Could not read state: {e}")
    return DEFAULT_START

def save_fetch_date(key: str):
    state = {}
    if os.path.exists(FETCH_STATE_FILE):
        try:
            with open(FETCH_STATE_FILE, encoding='utf-8') as f:
                state = json.load(f)
        except Exception:
            pass
    state[key] = date.today().isoformat()
    with open(FETCH_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)
    print(f"[state] Saved last-run date for '{key}': {state[key]}")


# ── OAuth (client_credentials, 2h TTL) ────────────────────────────────────────

def _load_cached_token() -> str | None:
    if not os.path.exists(TOKEN_CACHE_FILE):
        return None
    try:
        with open(TOKEN_CACHE_FILE) as f:
            cache = json.load(f)
        exp = datetime.fromisoformat(cache['expires_at'])
        if exp > datetime.now(timezone.utc) + timedelta(minutes=5):
            return cache['access_token']
    except Exception:
        pass
    return None

def _save_cached_token(token: str, expires_in: int):
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    with open(TOKEN_CACHE_FILE, 'w') as f:
        json.dump({'access_token': token, 'expires_at': expires_at.isoformat()}, f)

def get_access_token(force_refresh: bool = False) -> str:
    if not force_refresh:
        cached = _load_cached_token()
        if cached:
            return cached

    if not CLIENT_KEY or not CLIENT_SECRET:
        sys.exit("ERROR: set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in .env")

    r = requests.post(
        OAUTH_URL,
        data={
            'client_key':    CLIENT_KEY,
            'client_secret': CLIENT_SECRET,
            'grant_type':    'client_credentials',
        },
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    token   = data['access_token']
    expires = int(data.get('expires_in', 7200))
    _save_cached_token(token, expires)
    print(f"[auth] New token issued, expires in {expires}s")
    return token


# ── API calls ─────────────────────────────────────────────────────────────────

class RateLimitExceeded(Exception):
    """Raised when persistent 429s indicate quota is exhausted —
    caller should pause or back off, not just retry harder.
    """


def _parse_retry_after(header_val: str | None) -> float | None:
    """Retry-After is either an int (seconds) or an HTTP-date. We only handle
    the int form — TikTok in practice sends seconds when it sends one at all."""
    if not header_val:
        return None
    try:
        return max(0.0, float(header_val.strip()))
    except (TypeError, ValueError):
        return None


def _is_rate_limit_response(r: requests.Response) -> bool:
    """429, or 200/4xx with a TikTok error body whose code is rate_limit_exceeded
    (defensive — some APIs return non-429 status with rate-limit error codes)."""
    if r.status_code == 429:
        return True
    try:
        body = r.json()
    except Exception:
        return False
    code = (body.get('error') or {}).get('code', '')
    return code == 'rate_limit_exceeded'


def _api_post(url: str, params: dict, body: dict,
              max_retries: int = 3) -> dict:
    """Token refreshes only on 401. 429s honor Retry-After when present,
    fall back to exponential backoff with jitter otherwise. Persistent 429s
    raise RateLimitExceeded so callers can implement circuit-breaking.
    """
    backoff      = BACKOFF_BASE_429
    need_refresh = False
    r            = None

    for attempt in range(max_retries):
        token = get_access_token(force_refresh=need_refresh)
        need_refresh = False
        r = requests.post(
            url,
            params=params,
            json=body,
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type':  'application/json',
            },
            timeout=30,
        )

        if r.status_code == 401 and attempt < max_retries - 1:
            need_refresh = True
            continue

        if _is_rate_limit_response(r):
            # Prefer the server's Retry-After. Add small jitter to avoid
            # everyone retrying on exactly the same second.
            retry_after = _parse_retry_after(r.headers.get('Retry-After'))
            wait = retry_after if retry_after is not None else backoff
            wait = min(wait, BACKOFF_MAX_429) + random.uniform(0, 2)
            src  = 'Retry-After' if retry_after is not None else 'backoff'
            print(f"    [429] rate-limited, sleeping {wait:.1f}s ({src}, attempt {attempt+1}/{max_retries})")
            time.sleep(wait)
            backoff = min(backoff * 2, BACKOFF_MAX_429)
            continue

        if not r.ok:
            try:
                err_body = r.json()
            except Exception:
                err_body = r.text[:500]
            raise RuntimeError(f"HTTP {r.status_code} from {url}: {err_body}")

        return r.json()

    # All retries exhausted. If the last response was a rate-limit signal,
    # surface as quota issue so the caller can cool down or bail.
    if r is not None and _is_rate_limit_response(r):
        raise RateLimitExceeded(f"persistent 429 at {url}")
    raise RuntimeError(f"API failed after {max_retries} retries: {url}")


def query_advertisers(search_term: str, max_count: int = 50) -> list[dict]:
    """Search the advertiser index by name. Returns list of
       {business_id, business_name, country_code}.
       Note: API has no country filter — we filter to CY client-side.
       Raises RateLimitExceeded so the caller can apply a cooldown;
       all other errors are logged and swallowed (returns []).
    """
    if not search_term or len(search_term) > 50:
        return []
    body = {
        'search_term': search_term,
        'max_count':   max_count,
    }
    params = {'fields': 'business_id,business_name,country_code'}
    try:
        data = _api_post(ADV_URL, params, body)
    except RateLimitExceeded:
        raise
    except Exception as e:
        print(f"    [advertisers] '{search_term}' failed: {e}")
        return []
    return data.get('data', {}).get('advertisers', [])


# Reach ranges → (lower_bound, upper_bound). TikTok returns strings
# like "1K-10K"; map both sides to ints. Falls back to (None, None) on
# unrecognised tokens. The mapping is conservative — adjust if the
# library starts reporting new tiers.
_REACH_UNITS = {'K': 1_000, 'M': 1_000_000, 'B': 1_000_000_000}

def _parse_reach_token(tok: str) -> int | None:
    tok = (tok or '').strip().replace(',', '')
    if not tok:
        return None
    m = re.match(r'^([\d.]+)\s*([KMB]?)$', tok, re.IGNORECASE)
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2).upper()
    return int(n * _REACH_UNITS.get(unit, 1))

def parse_reach(raw: str) -> tuple[int | None, int | None]:
    if not raw:
        return None, None
    if '-' in raw:
        lo, hi = raw.split('-', 1)
        return _parse_reach_token(lo), _parse_reach_token(hi)
    n = _parse_reach_token(raw)
    return n, n


def query_ads_for_advertiser(business_id: int,
                             since_date: str) -> list[dict]:
    """Paginate through all CY ads for a single advertiser since `since_date`."""
    # TikTok requires `max` to be strictly before today — use yesterday.
    yesterday = (date.today() - timedelta(days=1)).strftime('%Y%m%d')
    since     = date.fromisoformat(since_date).strftime('%Y%m%d')

    fields = ','.join([
        'ad.id', 'ad.first_shown_date', 'ad.last_shown_date',
        'ad.status', 'ad.status_statement',
        'ad.videos', 'ad.image_urls', 'ad.reach',
        'advertiser.business_id', 'advertiser.business_name', 'advertiser.paid_for_by',
    ])

    rows: list[dict] = []
    search_id = None
    while True:
        body: dict = {
            'filters': {
                'ad_published_date_range': {'min': since, 'max': yesterday},
                'country_code':            'CY',
                'advertiser_business_ids': [business_id],
            },
            'max_count': 50,
        }
        if search_id:
            body['search_id'] = search_id
        try:
            data = _api_post(ADS_URL, {'fields': fields}, body)
        except Exception as e:
            print(f"    [ads] business_id={business_id} failed: {e}")
            break
        payload   = data.get('data', {})
        ads       = payload.get('ads', [])
        rows.extend(ads)
        if not payload.get('has_more') or not payload.get('search_id'):
            break
        search_id = payload['search_id']
        time.sleep(REQUEST_DELAY)
    return rows


# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tiktok_ads (
            ad_id                     TEXT,
            advertiser_id             TEXT,
            advertiser_disclosed_name TEXT,
            ad_funded_by              TEXT,
            country_code              TEXT,
            ad_url                    TEXT,
            first_shown               TEXT,
            last_shown                TEXT,
            ad_status                 TEXT,
            status_statement          TEXT,
            videos_json               TEXT,
            image_urls_json           TEXT,
            reach_raw                 TEXT,
            times_shown_lower_bound   INTEGER,
            times_shown_upper_bound   INTEGER,
            targeting_json            TEXT,
            matched_candidate         TEXT,
            matched_party             TEXT,
            matched_district          TEXT,
            match_type                TEXT,
            is_political              INTEGER DEFAULT 1,
            checked_at                TEXT NOT NULL,
            PRIMARY KEY (ad_id)
        )
    """)
    conn.commit()
    conn.close()

def _fmt_date(yyyymmdd: str) -> str:
    """20260501 → 2026-05-01. Empty → ''."""
    if not yyyymmdd or len(yyyymmdd) != 8:
        return yyyymmdd or ''
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"

_UPSERT_COLS = [
    'ad_id', 'advertiser_id', 'advertiser_disclosed_name', 'ad_funded_by',
    'country_code', 'ad_url', 'first_shown', 'last_shown',
    'ad_status', 'status_statement', 'videos_json', 'image_urls_json',
    'reach_raw', 'times_shown_lower_bound', 'times_shown_upper_bound',
    'targeting_json', 'matched_candidate', 'matched_party',
    'matched_district', 'match_type', 'is_political', 'checked_at',
]

def upsert_rows(rows: list[dict]) -> int:
    """Insert-or-update by explicit column names. Uses ON CONFLICT(ad_id)
    so non-listed columns (e.g. transcript, targeting_age) are preserved
    when re-upserting an existing row — the old positional INSERT OR REPLACE
    silently NULL'd those out, which is bad when re-fetching after schema growth.

    Acquires a cross-process lock on the DB file so concurrent runs
    (e.g. manual discover + cron refresh) serialize cleanly. SQLite's
    own file lock prevents corruption, but two writers can still race
    on the side caches and lose UPDATEs to the same ad_id.
    """
    from db_lock import db_lock
    with db_lock(DB_PATH):
        return _upsert_rows_unlocked(rows)


def _upsert_rows_unlocked(rows: list[dict]) -> int:
    """Inner upsert without lock acquisition. Called by upsert_rows()
    or by callers that already hold the lock (e.g. when batching multiple
    upserts inside one outer transaction)."""
    conn  = sqlite3.connect(DB_PATH)
    now   = datetime.now(timezone.utc).isoformat()
    cols       = ",".join(_UPSERT_COLS)
    placeholders = ",".join("?" * len(_UPSERT_COLS))
    update_set = ",".join(f"{c}=excluded.{c}" for c in _UPSERT_COLS if c != 'ad_id')
    sql = f"""
        INSERT INTO tiktok_ads ({cols}) VALUES ({placeholders})
        ON CONFLICT(ad_id) DO UPDATE SET {update_set}
    """
    saved = 0
    for r in rows:
        try:
            conn.execute(sql, (
                r.get('ad_id'),
                r.get('advertiser_id'),
                r.get('advertiser_disclosed_name'),
                r.get('ad_funded_by'),
                r.get('country_code'),
                r.get('ad_url'),
                r.get('first_shown'),
                r.get('last_shown'),
                r.get('ad_status'),
                r.get('status_statement'),
                r.get('videos_json'),
                r.get('image_urls_json'),
                r.get('reach_raw'),
                r.get('times_shown_lower_bound'),
                r.get('times_shown_upper_bound'),
                r.get('targeting_json'),
                r.get('matched_candidate'),
                r.get('matched_party'),
                r.get('matched_district'),
                r.get('match_type'),
                r.get('is_political', 1),
                now,
            ))
            saved += 1
        except sqlite3.Error as e:
            print(f"  [db] skipped: {e}")
    conn.commit()
    conn.close()
    return saved


# ── Discovery ─────────────────────────────────────────────────────────────────

GENERIC_TERMS = [
    # Latin only — TikTok's search_term endpoint 500s on non-ASCII input.
    'AKEL', 'DISY', 'DIKO', 'EDEK', 'ELAM', 'DIPA', 'VOLT', 'ALMA',
    'Cyprus election', 'Kypros ekloges',
]

def build_search_terms(candidates_path: str) -> list[str]:
    """Latin-transliterated candidate last names + party + generic terms."""
    terms: set[str] = set(GENERIC_TERMS)
    with open(candidates_path, encoding='utf-8') as f:
        for c in csv.DictReader(f):
            name = c.get('name', '').strip()
            if not name:
                continue
            parts = name.split()
            if parts:
                last_latin = name_to_latin(parts[0])
                if 4 <= len(last_latin) <= 50:
                    terms.add(last_latin)
                if len(parts) >= 2:
                    first_latin = name_to_latin(parts[-1])
                    if 5 <= len(first_latin) <= 50:
                        terms.add(first_latin)
    return sorted(terms)


def _load_discovered_cache() -> tuple[dict, set]:
    """Returns (advertisers_by_bid, tried_terms_set). Empty if no cache yet."""
    if not os.path.exists(DISCOVERED_CACHE):
        return {}, set()
    try:
        with open(DISCOVERED_CACHE, encoding='utf-8') as f:
            data = json.load(f)
        # JSON keys are strings; advertisers dict keyed by str(bid).
        advs = {str(k): v for k, v in data.get('advertisers', {}).items()}
        return advs, set(data.get('tried_terms', []))
    except Exception as e:
        print(f"  [cache] could not read {DISCOVERED_CACHE}: {e}")
        return {}, set()

def _save_discovered_cache(advertisers: dict, tried_terms: set):
    payload = {
        'saved_at':    datetime.now(timezone.utc).isoformat(),
        'advertisers': {str(k): v for k, v in advertisers.items()},
        'tried_terms': sorted(tried_terms),
    }
    tmp = DISCOVERED_CACHE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, DISCOVERED_CACHE)


def discover_cy_advertisers(search_terms: list[str],
                            limit: int | None = None,
                            reset: bool = False) -> dict[str, dict]:
    """Search each term, collect unique CY business_ids → {bid_str: advertiser dict}.
       Resumable: state persisted to tiktok_discovered.json after every term.
       Circuit-breaker: pauses COOLDOWN_429 seconds after COOLDOWN_TRIGGER 429s.
    """
    if reset:
        for path in (DISCOVERED_CACHE, DISCOVERED_CACHE + '.tmp'):
            if os.path.exists(path):
                os.remove(path)
        print(f"  [cache] reset — starting discovery from scratch")

    found, tried = _load_discovered_cache()
    if found or tried:
        print(f"  [cache] resuming with {len(found)} advertisers, {len(tried)} terms already tried")

    pending = [t for t in search_terms if t not in tried]
    if limit:
        pending = pending[:limit]
    if not pending:
        print(f"  [discover] all {len(search_terms)} terms already tried — nothing to do")
        return found

    print(f"  [discover] {len(pending)} new terms to query (skipping {len(tried)} cached)")
    consecutive_429 = 0
    dry_cooldowns   = 0   # cooldowns taken in a row with zero successful terms between them

    for i, term in enumerate(pending, 1):
        try:
            results = query_advertisers(term, max_count=50)
            consecutive_429 = 0
            dry_cooldowns   = 0   # any successful call resets the bail counter
        except RateLimitExceeded:
            consecutive_429 += 1
            print(f"    [429] persistent — consecutive count = {consecutive_429}")
            if consecutive_429 >= COOLDOWN_TRIGGER:
                dry_cooldowns += 1
                if dry_cooldowns > MAX_COOLDOWNS:
                    print(f"    [bail] {MAX_COOLDOWNS} dry cooldowns in a row — "
                          f"quota looks genuinely dead. Saving state and exiting; "
                          f"try again in a few hours.")
                    _save_discovered_cache(found, tried)
                    return found
                print(f"    [cooldown {dry_cooldowns}/{MAX_COOLDOWNS}] sleeping {COOLDOWN_429}s")
                _save_discovered_cache(found, tried)
                time.sleep(COOLDOWN_429)
                consecutive_429 = 0
            # Do NOT mark term as tried — retry it next pass.
            continue

        for adv in results:
            if (adv.get('country_code') or '').upper() == 'CY':
                bid = str(adv.get('business_id'))
                if bid and bid not in found:
                    found[bid] = adv
                    print(f"    + CY advertiser: {adv.get('business_name')} (bid={bid}) [via '{term}']")

        tried.add(term)
        if i % 10 == 0:
            _save_discovered_cache(found, tried)
            print(f"    ... {i}/{len(pending)} new terms searched, {len(found)} CY advertisers total")
        time.sleep(REQUEST_DELAY)

    _save_discovered_cache(found, tried)
    print(f"  [discover] Found {len(found)} unique CY advertisers total (across all runs)")
    return found


# ── Main ──────────────────────────────────────────────────────────────────────

def run(args):
    if not CLIENT_KEY or not CLIENT_SECRET:
        sys.exit("ERROR: TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET missing from .env")

    since_date = DEFAULT_START if args.full else load_fetch_since('tiktok_discover')
    print(f"\nDiscover TikTok ads — Cyprus, since {since_date}")
    print("=" * 65)

    get_access_token()   # prime the cache so first query doesn't 401
    cand_index = load_candidate_index(CANDIDATES_FILE)
    print(f"  Loaded {len(cand_index)} candidates")

    if not args.dry_run:
        init_db()

    # 1. Discover CY advertisers (resumable; --full or --reset-cache wipes state)
    terms      = build_search_terms(CANDIDATES_FILE)
    cy_advs    = discover_cy_advertisers(terms, limit=args.limit,
                                         reset=(args.full or args.reset_cache))
    if not cy_advs:
        print("  No CY advertisers found in TikTok library.")
        print("  (Reminder: TikTok bans paid political ads — zero hits is expected.)")
        return

    # 2. Filter to political-ish (name match against candidates OR party keyword)
    political_advs: list[dict] = []
    for adv in cy_advs.values():
        name = adv.get('business_name', '')
        m    = match_advertiser(name, '', cand_index)
        if m or advertiser_name_is_political(name):
            political_advs.append(adv)
    print(f"  [filter] {len(political_advs)}/{len(cy_advs)} match a candidate/party")

    if not political_advs:
        print("  No political-looking CY advertisers found.")
        return

    # 3. Fetch ads for each political advertiser
    all_rows: list[dict] = []
    summary: dict[str, dict] = {}
    print()
    for adv in political_advs:
        name = adv.get('business_name', '')
        bid  = adv.get('business_id')
        print(f"  Fetching ads for {name} (bid={bid})...")
        ads = query_ads_for_advertiser(bid, since_date)
        print(f"    → {len(ads)} ads")

        match = match_advertiser(name, '', cand_index)
        for item in ads:
            ad_obj = item.get('ad', {}) or {}
            av_obj = item.get('advertiser', {}) or {}
            reach_raw = (ad_obj.get('reach') or {}).get('unique_users_seen') or ''
            lb, ub = parse_reach(reach_raw)
            ad_id = str(ad_obj.get('id') or '')

            # Numeric-business_name quirk — see tiktok_api.resolve_disclosed_name
            # docstring for the full explanation. We centralize the workaround
            # in tiktok_api.py so every reader/writer uses the same logic.
            from tiktok_api import resolve_disclosed_name, resolve_funded_by
            disclosed_name  = resolve_disclosed_name(av_obj, fallback=name)
            funded_by_value = resolve_funded_by(av_obj)

            row = {
                'ad_id':                     ad_id,
                'advertiser_id':             str(av_obj.get('business_id') or bid),
                'advertiser_disclosed_name': disclosed_name,
                'ad_funded_by':              funded_by_value,
                'country_code':              'CY',
                'ad_url':                    f"https://library.tiktok.com/ads/detail/?ad_id={ad_id}" if ad_id else None,
                'first_shown':               _fmt_date(ad_obj.get('first_shown_date', '')),
                'last_shown':                _fmt_date(ad_obj.get('last_shown_date', '')),
                'ad_status':                 ad_obj.get('status'),
                'status_statement':          ad_obj.get('status_statement'),
                'videos_json':               json.dumps(ad_obj.get('videos') or [], ensure_ascii=False),
                'image_urls_json':           json.dumps(ad_obj.get('image_urls') or [], ensure_ascii=False),
                'reach_raw':                 reach_raw,
                'times_shown_lower_bound':   lb,
                'times_shown_upper_bound':   ub,
                'targeting_json':            None,   # populated by separate ad-details pass if added later
                'matched_candidate':         match['name']       if match else None,
                'matched_party':             match['party']      if match else None,
                'matched_district':          match['district']   if match else None,
                'match_type':                match['match_type'] if match else None,
                'is_political':              1,
            }
            all_rows.append(row)

        key = match['name'] if match else f"[UNKNOWN] {name}"
        summary.setdefault(key, {
            'party':    match['party']    if match else '—',
            'district': match['district'] if match else '—',
            'ads': 0, 'matched': match is not None,
        })
        summary[key]['ads'] += len(ads)
        time.sleep(REQUEST_DELAY)

    # 4. Save
    if not args.dry_run:
        saved = upsert_rows(all_rows)
        print(f"\n→ {saved} rows saved to tiktok_ads in {DB_PATH}")
        save_fetch_date('tiktok_discover')
    else:
        print(f"\n[dry-run] Would save {len(all_rows)} rows.")

    # 5. Report
    print("\n" + "=" * 65)
    print("DISCOVERY REPORT — Cyprus Political TikTok Ads")
    print("=" * 65)
    matched   = {k: v for k, v in summary.items() if v['matched']}
    unmatched = {k: v for k, v in summary.items() if not v['matched']}
    print(f"\n✅ Matched to candidates.csv ({len(matched)}):")
    for name, info in sorted(matched.items(), key=lambda x: -x[1]['ads']):
        print(f"   {info['ads']:>3} ads  {name} ({info['party']} — {info['district']})")
    if unmatched:
        print(f"\n🆕 NEW — not in candidates.csv ({len(unmatched)}):")
        for name, info in sorted(unmatched.items(), key=lambda x: -x[1]['ads']):
            print(f"   {info['ads']:>3} ads  {name}")
    print("\n⚠  Reminder: TikTok bans paid political ads. Any non-zero hits here\n"
          "   are likely policy violations and worth manual review.")
    print("=" * 65)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--full',    action='store_true',
                        help='Re-fetch from DEFAULT_START AND wipe the discovery cache')
    parser.add_argument('--dry-run', action='store_true', help='No DB writes')
    parser.add_argument('--limit',   type=int, default=None,
                        help='Only check first N search terms (debugging)')
    parser.add_argument('--reset-cache', action='store_true',
                        help='Wipe tiktok_discovered.json before discovery')
    args = parser.parse_args()

    if not os.path.exists(CANDIDATES_FILE):
        sys.exit(f"ERROR: {CANDIDATES_FILE} not found")
    run(args)


if __name__ == '__main__':
    main()
