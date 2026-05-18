# Cyprus 2026 — TikTok Political Ad Monitor

[![Live dashboard](https://img.shields.io/badge/dashboard-live-success?logo=streamlit&logoColor=white)](https://cyprus-2026-tiktok.streamlit.app/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](#license)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://python.org)

🔗 **Live dashboard → https://cyprus-2026-tiktok.streamlit.app/**

End-to-end pipeline + Streamlit dashboard tracking political advertising on TikTok
ahead of the **Cyprus 2026 parliamentary elections**.

TikTok officially bans political advertising globally — this project surfaces ads
that slip through anyway, by querying the TikTok Commercial Content API, matching
advertisers against the candidate roster, and presenting results in a public
dashboard.

## What's inside

| File | Purpose |
|---|---|
| `discover_tiktok_ads.py` | Pipeline core — OAuth, advertiser search, ad fetching, DB schema |
| `discover_content_keywords.py` | 130-keyword sweep across the ad-content API |
| `classify_ads.py` | Match advertisers to candidates / parties / districts |
| `smarter_candidate_match.py` | Greek↔Latin transliteration matcher with variants |
| `auto_bio_scan.py` | Playwright-based bulk profile bio scanner |
| `export_profiles.py` | Generate `tiktok_profiles.xlsx` — one row per advertiser |
| `export_verify_recent_ads.py` | Generate `tiktok_verify_recent_ads.xlsx` for spot-checks |
| `export_tiktok_excel.py` | Bulk TikTok ToS-violation report |
| `app_tiktok.py` | **Streamlit dashboard** — 6 tabs: Overview, By party, By candidate, Browse ads, Transcript search, Raw data |
| `candidates.csv` | Public ballot data — 746 candidates across 19 parties |
| `politician_ads_public.db` | Sanitised SQLite snapshot — 482 ads, 73 advertisers (candidates + party accounts + media) |


## Local setup

```bash
git clone https://github.com/<your-username>/cyprus-2026-tiktok-monitor.git
cd cyprus-2026-tiktok-monitor
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium    # for bio scanning

# Copy template and fill in your TikTok / Meta keys
cp .env.example .env
# edit .env

# Launch the dashboard against the public DB snapshot
streamlit run app_tiktok.py
```

## Running a discovery sweep

```bash
# Requires TIKTOK_CLIENT_KEY + TIKTOK_CLIENT_SECRET in .env
# (Research API access required — apply at https://developers.tiktok.com/)
python discover_tiktok_ads.py --full
python discover_content_keywords.py
python classify_ads.py
python export_profiles.py
```

## Current state (snapshot 2026-05-18)

| Category | Count |
|---|---|
| Candidate | 56 |
| Party account | 2 |
| Party supporter | 4 |
| Commentator | 2 |
| News outlet | 2 |
| Political movement | 1 |
| Politician (non-candidate) | 1 |
| Likely false positive | 36 |
| Content-keyword hit (unclassified) | 322 |

## Methodology

We use 8 identification methods, ranked by reliability (full ranking + lessons learned in `DEPLOY_GUIDE.md`):

1. **Playwright bio scan (mobile UA)** — catches active campaigners with explicit "Υποψήφι..." bios
2. **Ad creative extraction → frame analysis** — catches empty-bio candidates via party iconography
3. **DB transcript scan** — catches candidates who speak district/party names on camera
4. **`candidates.csv` direct match** — confirms but rarely unique (many homonyms)
5. **Transliteration matcher** (κ↔c, χ↔h, θ↔t, φ↔ph, υ↔y/u/i)
6. **Surname + first-initial pattern**
7. **Funder cross-reference** — confirmed structurally unviable (each CY candidate has unique funder due to campaign-finance law)
8. **TikTok oEmbed / WebFetch** — CAPTCHA-walled

## Deployment

See `DEPLOY_GUIDE.md` for step-by-step instructions on:
- Pushing to GitHub
- Deploying to Streamlit Community Cloud
- Self-hosting on a VPS with daily cron

## License

MIT
