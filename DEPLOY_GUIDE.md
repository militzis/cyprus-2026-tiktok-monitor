# Deploy the TikTok dashboard publicly

## Part 1 — Push to GitHub

### 1.1 Pre-flight: things that MUST NOT be committed
Sensitive files (TikTok API keys, OAuth tokens, Playwright user-data dirs, the live DB
with thousands of advertiser rows). We'll write a `.gitignore` to keep them out.

### 1.2 Initialize the repo

```powershell
# In the project root
cd "C:\Users\milit\OneDrive\Documents\META library content"

# Initialize git if not already
git init -b main

# Set your identity (one-time)
git config user.name "Your Name"
git config user.email "you@example.com"
```

### 1.3 Create `.gitignore`

```
# === Secrets ===
.env
*.token
*.key
credentials.json
secrets.json

# === Local data (too big / private) ===
*.db
*.db-journal
*.db-wal
*.db-shm
politician_ads.db
content_keyword_discovery.json
tiktok_handle_resolution.json
tiktok_bio_scan.json
*.mp4
*.jpg
*.jpeg
*.png
*.xlsx
.pw_*/
.pw_profile_*/
__pycache__/
*.pyc
.venv/
venv/

# === Ad-hoc throwaway scripts ===
_*.py

# === OS ===
.DS_Store
Thumbs.db
```

### 1.4 Decide which scripts to publish

Recommended **public** scripts (curated, useful, no secrets):
- `discover_tiktok_ads.py` — main pipeline (keep, but redact any embedded keys)
- `discover_content_keywords.py` — keyword sweep
- `discover_google_ads.py` — Google Ads variant
- `classify_ads.py` — classifier
- `auto_bio_scan.py` — Playwright bio scanner
- `smarter_candidate_match.py` — transliteration matcher
- `export_profiles.py` + `export_verify_recent_ads.py` + `export_tiktok_excel.py`
- `app_tiktok.py` — Streamlit dashboard
- `candidates.csv` — public ballot data
- `requirements.txt` (new — see 1.6 below)
- `README.md` (new — see 1.7 below)

### 1.5 Auth: move ALL secrets out of code into env vars

In `discover_tiktok_ads.py` (and any other file with embedded keys), replace
hard-coded credentials with `os.environ.get()`:

```python
TIKTOK_CLIENT_KEY = os.environ['TIKTOK_CLIENT_KEY']
TIKTOK_CLIENT_SECRET = os.environ['TIKTOK_CLIENT_SECRET']
```

Then create a local `.env` (gitignored) with the actual values, and load it with
`python-dotenv`.

### 1.6 Generate `requirements.txt`

```powershell
pip freeze | Out-File -Encoding utf8 requirements.txt
```

Or trim to just what the public scripts use:

```
requests>=2.31
openpyxl>=3.1
streamlit>=1.30
pandas>=2.0
faster-whisper>=1.0
playwright>=1.40
opencv-python-headless>=4.8
python-dotenv>=1.0
```

### 1.7 Write a `README.md`

Cover: what the project does, how to install, how to fire a discovery sweep, how
to launch the dashboard. Keep it short.

### 1.8 First commit + push

```powershell
# Stage explicitly (NEVER use `git add .` here — could include secrets)
git add .gitignore requirements.txt README.md DEPLOY_GUIDE.md `
        discover_tiktok_ads.py discover_content_keywords.py discover_google_ads.py `
        classify_ads.py auto_bio_scan.py smarter_candidate_match.py `
        export_profiles.py export_verify_recent_ads.py export_tiktok_excel.py `
        app_tiktok.py candidates.csv

git status   # verify NO _*.py, NO .db, NO .mp4, NO credentials
git commit -m "Initial public commit: Cyprus 2026 TikTok ad monitor"
```

### 1.9 Create the GitHub repo + push

Two options:

**Option A — via gh CLI** (recommended):
```powershell
# Install: https://cli.github.com/  OR  winget install --id GitHub.cli
gh auth login
gh repo create cyprus-2026-tiktok-monitor `
    --public `
    --description "Cyprus 2026 parliamentary elections — TikTok political ad monitoring" `
    --source . --remote origin --push
```

**Option B — via GitHub web**:
1. Go to https://github.com/new
2. Create repo `cyprus-2026-tiktok-monitor` (Public, no README)
3. Locally:
   ```powershell
   git remote add origin https://github.com/<your-username>/cyprus-2026-tiktok-monitor.git
   git push -u origin main
   ```

---

## Part 2 — Stream the dashboard publicly

Three good options. Easiest first.

### Option A — Streamlit Community Cloud (free, public, 1-click deploy)

**Requirements**: GitHub repo (Part 1 done), `app_tiktok.py`, `requirements.txt`.

**Caveat**: Your dashboard reads `politician_ads.db` which is gitignored. You have two choices:
- **A1 — Commit a snapshot of the DB** (acceptable for a public dashboard; data is already public political-ad info, and FPs you've flagged. Anonymise the FP business names first if needed.)
- **A2 — Use Streamlit secrets to access a remote DB** (Postgres on Neon/Supabase — see Option B/C below)

**Steps for A1:**

1. **Commit a sanitised DB snapshot**:
   ```powershell
   # Make a copy and drop the noisy content_keyword limbo rows + FP rows
   python -c "import sqlite3, shutil; shutil.copy(r'C:\Users\milit\meta_pipeline_data\politician_ads.db', 'politician_ads_public.db'); c=sqlite3.connect('politician_ads_public.db'); c.execute(\"DELETE FROM tiktok_ads WHERE match_type IN ('content_keyword', 'likely_false_positive_business', 'likely_false_positive_personal')\"); c.commit(); c.close(); print('public snapshot ready')"

   # Update .gitignore to ALLOW the public snapshot
   # add a line:  !politician_ads_public.db
   git add politician_ads_public.db .gitignore
   git commit -m "Add public-safe DB snapshot for dashboard"
   git push
   ```

2. **Update `app_tiktok.py`** to read from `politician_ads_public.db` when the env
   var `POLITICIAN_ADS_DB` isn't set:
   ```python
   import os
   DB_PATH = os.environ.get('POLITICIAN_ADS_DB', 'politician_ads_public.db')
   ```

3. **Deploy**:
   - Go to https://share.streamlit.io/
   - Sign in with GitHub
   - "New app" → pick your repo + branch `main` + main file `app_tiktok.py`
   - Click Deploy
   - You get a public URL like `https://cyprus-2026-tiktok-monitor.streamlit.app/`

4. **Share that URL**. Anyone with the link can use the dashboard.

### Option B — Hugging Face Spaces (free, also one-click)

Same idea, different host:
1. Create a Space at https://huggingface.co/new-space
2. Type: "Streamlit"
3. Either link your GitHub repo or push directly to the HF Git remote
4. Public URL: `https://huggingface.co/spaces/<user>/<space>`

HF gives more compute headroom than Streamlit Cloud's free tier.

### Option C — Self-hosted on a VPS (full control, costs money)

For a more permanent setup with always-fresh data:

1. **Spin up** a small VPS (Hetzner CX11 ~€4/mo, DigitalOcean $6/mo)
2. **Run** the discovery pipeline on a daily cron
3. **Serve** the Streamlit app behind nginx with a domain
4. **Schedule** discovery + transcription:
   ```bash
   # /etc/cron.d/tiktok-pipeline
   0 4 * * * cyprusbot cd /opt/cyprus-monitor && python discover_content_keywords.py
   30 4 * * * cyprusbot cd /opt/cyprus-monitor && python transcribe_tiktok_creatives.py
   ```

This is the most work but gives you fresh data every morning instead of a static snapshot.

---

## Recommended path for the user
1. Do Part 1 (GitHub push) — gets the code public, others can fork/contribute
2. Do Part 2 Option A1 (Streamlit Cloud + DB snapshot) — gets the dashboard live within 30 minutes
3. If the project gets traction, upgrade to Option C for live data
