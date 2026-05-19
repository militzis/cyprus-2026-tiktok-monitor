"""Find the canonical tiktok.com/@<handle>/video|photo/<post_id> URL for each
ad in the DB by scraping the advertiser's public profile page.

TikTok's Ad Library deliberately strips the post_id from the rendered HTML,
so reverse-lookup from `ad_id` is impossible. The only path is:
  1. Open the advertiser's profile feed at https://www.tiktok.com/@<handle>
  2. Scroll until enough posts are loaded
  3. For each post in the feed: extract post_id + post_date + thumbnail URL
  4. Match each DB ad to a profile post by (handle, date proximity)

Caveats:
  - Slow (browser rendering + scroll)
  - Brittle (TikTok rate-limits; CAPTCHA shows up under load)
  - Lossy: if the advertiser deleted the post after it ran as an ad,
    we'll never find a match
  - Date matching is approximate: an ad's first_shown date should be close to
    the post's creation date, but TikTok can boost posts weeks after they were
    organically posted

Usage:
  python find_canonical_post_urls.py                            # all candidate handles
  python find_canonical_post_urls.py --handle argentoulaioannou # one handle
  python find_canonical_post_urls.py --limit 5                  # only first 5 handles
  python find_canonical_post_urls.py --output reports/canonical_urls.csv

Schema additions:
  Adds column `canonical_post_url` to tiktok_ads if missing.
"""
import os, sys, sqlite3, asyncio, argparse, re, json, time
from datetime import datetime
from collections import defaultdict
sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None
from playwright.async_api import async_playwright

DB = os.environ.get('POLITICIAN_ADS_DB',
                    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 'politician_ads_public.db'))


def ensure_schema(conn: sqlite3.Connection):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tiktok_ads)").fetchall()]
    if 'canonical_post_url' not in cols:
        conn.execute("ALTER TABLE tiktok_ads ADD COLUMN canonical_post_url TEXT;")
    if 'canonical_post_id' not in cols:
        conn.execute("ALTER TABLE tiktok_ads ADD COLUMN canonical_post_id TEXT;")
    if 'last_canonical_lookup' not in cols:
        conn.execute("ALTER TABLE tiktok_ads ADD COLUMN last_canonical_lookup TEXT;")
    conn.commit()


async def scrape_profile_posts(page, handle: str, max_scrolls: int = 6) -> list[dict]:
    """Return [{post_id, kind, url, date_str}, ...] for the advertiser's
    publicly visible posts."""
    url = f'https://www.tiktok.com/@{handle}'
    try:
        await page.goto(url, wait_until='domcontentloaded', timeout=30000)
    except Exception as e:
        print(f"    goto warn: {e}")
        return []
    await page.wait_for_timeout(4500)
    for sel in ['button:has-text("Decline")', 'button:has-text("Got it")']:
        try: await page.locator(sel).first.click(timeout=1500); await page.wait_for_timeout(400)
        except Exception: pass

    # Scroll to load lazy-loaded posts
    for _ in range(max_scrolls):
        await page.mouse.wheel(0, 4000)
        await page.wait_for_timeout(1500)

    # Extract every /video/<id> or /photo/<id> link
    try:
        hrefs = await page.eval_on_selector_all(
            'a[href*="/video/"], a[href*="/photo/"]',
            'els => els.map(e => e.href)'
        )
    except Exception as e:
        print(f"    href extract err: {e}")
        return []

    posts = []
    seen = set()
    for h in hrefs:
        m = re.match(r'(https://www\.tiktok\.com/@[\w._-]+/(video|photo)/(\d+))', h)
        if not m: continue
        url, kind, post_id = m.group(1), m.group(2), m.group(3)
        if post_id in seen: continue
        seen.add(post_id)
        # TikTok post IDs encode creation timestamp in the upper bits
        # (Snowflake-style); decode to get the post date.
        try:
            # First 32 bits of post_id (after dividing by 2^32) = unix timestamp
            ts = int(post_id) >> 32
            date_str = datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d')
        except Exception:
            date_str = ''
        posts.append({'post_id': post_id, 'kind': kind, 'url': url, 'date': date_str})
    return posts


def match_ads_to_posts(ads: list[dict], posts: list[dict]) -> list[tuple]:
    """For each ad, find the best matching post by date proximity.
    Returns [(ad_id, post_url, post_id, days_apart), ...]"""
    matches = []
    used_post_ids = set()
    for ad in ads:
        if not ad['first_shown']:
            continue
        try:
            ad_date = datetime.strptime(ad['first_shown'][:10], '%Y-%m-%d')
        except ValueError:
            continue
        best = None
        best_days = 9999
        for p in posts:
            if p['post_id'] in used_post_ids:
                continue
            if not p['date']:
                continue
            try:
                post_date = datetime.strptime(p['date'], '%Y-%m-%d')
            except ValueError:
                continue
            days = abs((ad_date - post_date).days)
            # Ads usually start within a few days of when the post was published
            if days < best_days:
                best_days = days
                best = p
        # Accept matches within 14 days. Tighten/loosen as needed.
        if best and best_days <= 14:
            matches.append((ad['ad_id'], best['url'], best['post_id'], best_days))
            used_post_ids.add(best['post_id'])
    return matches


async def main(args):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    # Pick handles to scan
    if args.handle:
        handles = [args.handle]
    else:
        # All candidate / party / supporter handles with ads, that don't yet have
        # canonical_post_url filled in
        sel = """SELECT DISTINCT advertiser_disclosed_name AS h
                 FROM tiktok_ads
                 WHERE match_type IN ('manual_resume','party_account','party_supporter',
                                       'party_coordinator','political_movement',
                                       'politician_non_candidate')
                   AND advertiser_disclosed_name IS NOT NULL
                   AND advertiser_disclosed_name != ''
                   AND advertiser_disclosed_name NOT GLOB '[0-9]*'"""
        handles = [r['h'] for r in conn.execute(sel).fetchall()]
    if args.limit:
        handles = handles[:args.limit]
    print(f"  handles to scan: {len(handles)}")

    found_total = 0
    no_match_total = 0
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(
            viewport={'width': 1280, 'height': 2400},
            user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1',
            locale='el-CY',
        )
        page = await ctx.new_page()

        for i, handle in enumerate(handles, 1):
            print(f"\n  [{i}/{len(handles)}] @{handle}")
            ads = conn.execute("""SELECT ad_id, first_shown FROM tiktok_ads
                                  WHERE advertiser_disclosed_name=?
                                    AND canonical_post_url IS NULL""", (handle,)).fetchall()
            ads = [dict(r) for r in ads]
            if not ads:
                print(f"    all ads already have canonical URLs; skipping")
                continue
            print(f"    {len(ads)} ads need a canonical URL")

            posts = await scrape_profile_posts(page, handle, max_scrolls=args.scrolls)
            print(f"    found {len(posts)} posts on profile")
            if not posts:
                continue
            matches = match_ads_to_posts(ads, posts)
            print(f"    matched {len(matches)} / {len(ads)} ads")

            now = datetime.utcnow().isoformat()
            for ad_id, url, post_id, days in matches:
                conn.execute("""UPDATE tiktok_ads
                                SET canonical_post_url=?,
                                    canonical_post_id=?,
                                    last_canonical_lookup=?
                                WHERE ad_id=?""", (url, post_id, now, ad_id))
                print(f"      ✓ ad {ad_id} → {url}  (Δ {days}d)")
            for ad in ads:
                if not any(m[0] == ad['ad_id'] for m in matches):
                    conn.execute("UPDATE tiktok_ads SET last_canonical_lookup=? WHERE ad_id=?",
                                 (now, ad['ad_id']))
                    no_match_total += 1
            found_total += len(matches)
            conn.commit()
            time.sleep(2)   # be nice to TikTok

        await b.close()

    conn.close()
    print(f"\n  ── done ──  matched: {found_total}  unmatched: {no_match_total}")


def _parse():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--handle', default='', help='Scan only this one handle')
    ap.add_argument('--limit',  type=int, default=0, help='Cap to N handles')
    ap.add_argument('--scrolls', type=int, default=6,
                    help='How many times to scroll the profile (default 6 → ~30-50 posts)')
    return ap.parse_args()


if __name__ == '__main__':
    asyncio.run(main(_parse()))
