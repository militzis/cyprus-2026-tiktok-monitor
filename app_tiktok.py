"""TikTok political-ads dashboard — Cyprus 2026.

Run with:
    streamlit run app_tiktok.py

Reads from the non-OneDrive DB at C:\\Users\\milit\\meta_pipeline_data\\
(override via env vars POLITICIAN_ADS_DB / TIKTOK_CREATIVES_DIR).
"""
import os, sys, sqlite3, json, re
from collections import defaultdict, Counter
from datetime import date, datetime
import pandas as pd
import streamlit as st

# ── Config ────────────────────────────────────────────────────────────────────
# Resolve DB path with fallback chain:
#  1. POLITICIAN_ADS_DB env var (dev: points at full local DB)
#  2. ./politician_ads_public.db sibling to this file (public Streamlit deploy)
#  3. legacy Windows path (dev machine fallback)
_HERE = os.path.dirname(os.path.abspath(__file__))
_PUBLIC_DB = os.path.join(_HERE, 'politician_ads_public.db')
_LEGACY_DB = r'C:\Users\milit\meta_pipeline_data\politician_ads.db'

if os.environ.get('POLITICIAN_ADS_DB'):
    DB = os.environ['POLITICIAN_ADS_DB']
elif os.path.exists(_PUBLIC_DB):
    DB = _PUBLIC_DB
else:
    DB = _LEGACY_DB

CREATIVES = os.environ.get('TIKTOK_CREATIVES_DIR',
                           os.path.join(_HERE, 'creatives'))
CANDIDATES_CSV = os.path.join(_HERE, 'candidates.csv')

if not os.path.exists(DB):
    import streamlit as st
    st.error(f"Database not found at {DB}. "
             f"On Streamlit Cloud, the public snapshot `politician_ads_public.db` "
             f"should sit next to this script.")
    st.stop()

st.set_page_config(page_title="TikTok ads — Cyprus 2026", layout="wide", page_icon="🎯")

# ── Cached DB load ────────────────────────────────────────────────────────────
@st.cache_data(ttl=60)
def load_ads():
    c = sqlite3.connect(DB)
    # Probe for optional auto_review_* columns (added by auto_review.py).
    # If they don't exist yet, fall back to NULLs so the dashboard still
    # loads on a fresh deploy that hasn't been auto-reviewed yet.
    existing = {r[1] for r in c.execute("PRAGMA table_info(tiktok_ads)")}
    auto_cols = (
        "auto_review_verdict, auto_review_confidence, auto_review_reason, auto_review_at"
        if {'auto_review_verdict', 'auto_review_confidence',
            'auto_review_reason', 'auto_review_at'}.issubset(existing)
        else "NULL AS auto_review_verdict, NULL AS auto_review_confidence, "
             "NULL AS auto_review_reason, NULL AS auto_review_at"
    )
    # Same probe for the spend-estimate columns (compute_spend_estimates.py).
    # Lets the dashboard load on a fresh deploy that hasn't had the
    # script run yet.
    spend_cols = (
        "estimated_spend_eur_low, estimated_spend_eur_mid, estimated_spend_eur_high"
        if {'estimated_spend_eur_low', 'estimated_spend_eur_mid',
            'estimated_spend_eur_high'}.issubset(existing)
        else "NULL AS estimated_spend_eur_low, NULL AS estimated_spend_eur_mid, "
             "NULL AS estimated_spend_eur_high"
    )
    df = pd.read_sql_query(f"""
        SELECT advertiser_id, advertiser_disclosed_name AS handle,
               matched_candidate, matched_party, matched_district,
               ad_id, first_shown, last_shown, ad_status, status_statement,
               reach_raw, times_shown_lower_bound, times_shown_upper_bound,
               ad_funded_by, videos_json, image_urls_json,
               ad_url, transcript, match_type, checked_at,
               {auto_cols},
               {spend_cols}
        FROM tiktok_ads
    """, c)
    c.close()
    # Convert date columns
    for col in ('first_shown', 'last_shown'):
        df[col] = pd.to_datetime(df[col], errors='coerce')
    # Derive: kind, days_active
    def media_kind(row):
        try:
            v = json.loads(row['videos_json'] or '[]')
            i = json.loads(row['image_urls_json'] or '[]')
            return 'VIDEO' if v else ('IMAGE' if i else '?')
        except Exception:
            return '?'
    df['kind'] = df.apply(media_kind, axis=1)
    df['days_active'] = (df['last_shown'] - df['first_shown']).dt.days + 1
    df['profile_url'] = df['handle'].apply(
        lambda h: f"https://www.tiktok.com/@{h}" if h and not str(h).isdigit() else "")
    df['library_url'] = df['ad_id'].apply(
        lambda a: f"https://library.tiktok.com/ads/detail/?ad_id={a}")
    return df

@st.cache_data(ttl=60)
def load_candidates():
    if not os.path.exists(CANDIDATES_CSV):
        return pd.DataFrame()
    return pd.read_csv(CANDIDATES_CSV)

df = load_ads()
candidates_df = load_candidates()

# ── Sidebar filters ───────────────────────────────────────────────────────────
st.sidebar.header("Filters")

# Human-readable category groupings — these match what's in the DB's match_type
CATEGORY_LABELS = {
    'manual_resume':                    '🟢 Candidate',
    'party_account':                    '🟣 Party account',
    'party_supporter':                  '🟡 Party supporter',
    'party_coordinator':                '🟣 Party coordinator',
    'political_movement':               '🔴 Political movement',
    'politician_non_candidate':         '🔵 Politician (non-candidate)',
    'commentator':                      '🟠 Commentator',
    'news_outlet':                      '🟠 News outlet',
    'podcast':                          '🟠 Podcast',
    'satirist':                         '🟠 Satirist',
    'needs_profile_verification':       '❓ Needs verification',
    'content_keyword':                  '⚪ Content-keyword hit (unverified)',
    'likely_false_positive_business':   '✗ False positive (business)',
    'likely_false_positive_personal':   '✗ False positive (personal)',
    'likely_false_positive_homonym':    '✗ False positive (homonym)',
}
# Default: show everything in the political ecosystem (candidates, parties,
# supporters, media, movements). Hide content-keyword limbo + false positives.
DEFAULT_CATEGORIES = [
    'manual_resume', 'party_account', 'party_supporter', 'party_coordinator',
    'political_movement', 'politician_non_candidate',
    'commentator', 'news_outlet', 'podcast', 'satirist',
]

all_match_types = sorted(df['match_type'].dropna().unique().tolist())
default_match = [m for m in all_match_types if m in DEFAULT_CATEGORIES]
selected_match = st.sidebar.multiselect(
    "Category",
    all_match_types,
    default=default_match,
    format_func=lambda m: CATEGORY_LABELS.get(m, m),
    help=(
        "🟢 Candidate = confirmed on the ballot.  "
        "🟣 Party account = official party HQ.  "
        "🟡 Party supporter = activist account.  "
        "🟠 Media = commentator / news / podcast / satire.  "
        "⚪ Content-keyword = caught by sweep, not yet verified."
    ),
)

parties = ['(all)'] + sorted([p for p in df['matched_party'].dropna().unique()
                              if p and not p.startswith('[content-keyword')])
selected_party = st.sidebar.selectbox("Party", parties)

districts = ['(all)'] + sorted([d for d in df['matched_district'].dropna().unique() if d])
selected_district = st.sidebar.selectbox("District", districts)

status_opts = ['(all)'] + sorted(df['ad_status'].dropna().unique().tolist())
selected_status = st.sidebar.selectbox("Ad status", status_opts)

min_d = df['first_shown'].min()
max_d = df['last_shown'].max()
if pd.notna(min_d) and pd.notna(max_d):
    date_range = st.sidebar.date_input(
        "Active during", value=(min_d.date(), max_d.date()),
        min_value=min_d.date(), max_value=max_d.date(),
    )
else:
    date_range = None

# Apply filters
f = df.copy()
if selected_match:
    f = f[f['match_type'].isin(selected_match)]
if selected_party != '(all)':
    f = f[f['matched_party'] == selected_party]
if selected_district != '(all)':
    f = f[f['matched_district'] == selected_district]
if selected_status != '(all)':
    f = f[f['ad_status'] == selected_status]
if date_range and len(date_range) == 2:
    d_from, d_to = date_range
    f = f[(f['last_shown'] >= pd.Timestamp(d_from)) & (f['first_shown'] <= pd.Timestamp(d_to))]

# ── Page header ───────────────────────────────────────────────────────────────
st.title("🎯 TikTok Political Ads — Cyprus 2026")
st.caption(f"Last DB write: {df['checked_at'].max() if 'checked_at' in df.columns else '?'}")


# ── Pipeline health badge ─────────────────────────────────────────────────────
# Reads the most recent row from pipeline_health (written by
# refresh_ad_statuses.py at the end of each run). If older than 25h OR if
# the last run's status='failed', surface a red warning. Without this,
# silent cron failures would only be detected by humans noticing data
# going stale (today's bug class).
@st.cache_data(ttl=60)
def load_last_health():
    """Return (row, error_msg). row is None if the query couldn't run;
    error_msg explains why (so the badge can show the real cause instead
    of a silent fallback)."""
    try:
        conn = sqlite3.connect(DB)
        # Probe schema first — if the table doesn't exist yet (deploy hasn't
        # rebuilt with the T3-J change), say that explicitly.
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pipeline_health'"
        ).fetchone()
        if not exists:
            return None, "table 'pipeline_health' doesn't exist yet — deploy may not have rebuilt with the latest commit"
        row = conn.execute(
            "SELECT run_kind, finished_at, status, ads_checked, changes, "
            "errors, error_msg FROM pipeline_health "
            "ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row is None:
            return None, "pipeline_health table exists but is empty — no refresh has recorded a heartbeat yet"
        return row, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

_h, _h_err = load_last_health()
if _h is None:
    # Silent — when there's no health row yet (fresh deploy / first cron),
    # we don't want a yellow banner cluttering the page. The dashboard's
    # "Last DB write" caption already tells the user when data was last
    # touched. Red banners (failure/stale) still fire below.
    pass
else:
    _kind, _fin, _stat, _ads, _changes, _errs, _err_msg = _h
    _fin_ts = pd.to_datetime(_fin)
    _age_h  = (pd.Timestamp.utcnow().tz_localize(None) - _fin_ts).total_seconds() / 3600
    if _stat == 'failed':
        st.error(
            f"🚨 **Last pipeline run FAILED** ({_kind}, {_fin_ts:%Y-%m-%d %H:%M} UTC, "
            f"{_age_h:.1f}h ago). Error: `{_err_msg or '(no message)'}`. "
            f"Check the GitHub Actions log."
        )
    elif _age_h > 25:
        st.error(
            f"🚨 **Pipeline is STALE** — last successful refresh was "
            f"{_fin_ts:%Y-%m-%d %H:%M} UTC ({_age_h:.1f} h ago). The daily "
            f"cron may have stopped firing. Check GitHub Actions."
        )
    else:
        st.success(
            f"✅ Pipeline healthy — last {_kind} refresh "
            f"{_fin_ts:%Y-%m-%d %H:%M} UTC ({_age_h:.1f}h ago), "
            f"checked {_ads or 0} ads, {_changes or 0} change(s), "
            f"{_errs or 0} API error(s)."
        )

# ── Estimated total spend banner ──────────────────────────────────────────────
# Headline number — turns the abstract "TikTok runs banned political ads in
# Cyprus" into a quotable € figure. Mid estimate is the recommended single
# number; the low/high bounds are shown so readers know the precision limit.
def _spend_sum(col):
    s = f[col].sum() if col in f.columns else 0
    try:
        return int(s) if pd.notna(s) else 0
    except (TypeError, ValueError):
        return 0

_spend_low  = _spend_sum('estimated_spend_eur_low')
_spend_mid  = _spend_sum('estimated_spend_eur_mid')
_spend_high = _spend_sum('estimated_spend_eur_high')

if _spend_mid > 0:
    st.info(
        f"💶 **Estimated political-ad spend on TikTok in Cyprus:** "
        f"**€{_spend_mid:,}** (range €{_spend_low:,} – €{_spend_high:,}). "
        f"TikTok bans paid political ads globally — every euro shown here is "
        f"a likely policy violation.  "
        f"[*Methodology: TikTok's published reach buckets × EU commercial CPM "
        f"€3-€8/k.*]",
        icon="💶",
    )

# ── KPI row ───────────────────────────────────────────────────────────────────
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Total ads", len(f))
c2.metric("Unique advertisers", f['advertiser_id'].nunique())
c3.metric("🟢 Candidates", f[f['match_type'] == 'manual_resume']['advertiser_id'].nunique())
c4.metric("🟣 Party accounts", f[f['match_type'].isin(['party_account', 'party_coordinator'])]['advertiser_id'].nunique())
c5.metric("🟡 Supporters", f[f['match_type'] == 'party_supporter']['advertiser_id'].nunique())
c6.metric("🟠 Media/Commentators", f[f['match_type'].isin(['commentator', 'news_outlet', 'podcast', 'satirist'])]['advertiser_id'].nunique())

st.divider()

# ── Derived status: active / inactive / removed ──────────────────────────────
# Combine TikTok's reported status with date-based inference.
# Once refresh_ad_statuses.py runs, the `ad_status` column will hold the real
# value (active / inactive / removed_by_tiktok). For ads we haven't refreshed
# yet we fall back to a date-derived bucket.
import numpy as np
def derive_status(row, today=pd.Timestamp.today()):
    # NaN/NaT-safe field access: pandas iterrows() can return NaT or NaN
    # for missing fields; both are truthy in `X or ''` (so the fallback
    # doesn't kick in) but neither has .lower(). Use pd.notna() guards.
    raw_v  = row.get('ad_status')
    stmt_v = row.get('status_statement')
    raw  = (raw_v  if pd.notna(raw_v)  and isinstance(raw_v,  str) else '').lower()
    # TikTok takedowns: status often stays 'inactive' but the violation note
    # appears in status_statement ("Removed from TikTok due to a violation of
    # TikTok's terms"). Check both so we don't miss enforcement events.
    stmt = (stmt_v if pd.notna(stmt_v) and isinstance(stmt_v, str) else '').lower()
    if 'removed' in raw or 'removed' in stmt or 'violation' in raw or 'violation' in stmt:
        return '🚨 Removed by TikTok'
    if 'deleted' in raw or 'deleted_by_advertiser' in stmt:
        return '🗑 Deleted by advertiser'
    if raw == 'expired' or 'expired' in stmt:
        return '⌛ Expired'
    # date-derived
    ls = row.get('last_shown')
    if pd.isna(ls):
        return '❓ Unknown'
    days_since = (today - ls).days
    if days_since <= 7:
        return '✅ Active (last 7 days)'
    if days_since <= 30:
        return '🟡 Recently inactive (8–30 days)'
    return '⚪ Dormant (30+ days)'

df['derived_status'] = df.apply(derive_status, axis=1)
# Apply derived status to filtered df too — we need to redo this AFTER filters apply
f['derived_status'] = f.apply(derive_status, axis=1)

# ── Tabs ──────────────────────────────────────────────────────────────────────
(tab_overview, tab_enforce, tab_spend, tab_party, tab_candidates,
 tab_status, tab_review, tab_browse, tab_transcripts, tab_raw) = st.tabs([
    "📊 Overview", "🚨 Enforcement", "💶 Spend", "🏛 By party", "👤 By candidate",
    "🚦 Status & changes", "🔍 Review queue",
    "🎬 Browse ads", "📝 Transcript search", "🗂 Raw data",
])

# ── Overview ──────────────────────────────────────────────────────────────────
with tab_overview:
    st.subheader("Ad-launch timeline")
    if not f.empty and f['first_shown'].notna().any():
        timeline = f.groupby(pd.Grouper(key='first_shown', freq='W')).size().reset_index(name='ads')
        timeline.columns = ['week', 'ads']
        st.line_chart(timeline, x='week', y='ads', height=300)

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Top 15 advertisers (by ad count)")
        # Get most-recent ad_id per advertiser so we can deep-link to one of their ads
        latest_ad = (f.sort_values('last_shown', ascending=False)
                       .drop_duplicates('handle')[['handle', 'ad_id']]
                       .rename(columns={'ad_id': '_latest_ad_id'}))
        top = (f.groupby(['handle', 'matched_candidate', 'matched_party'])
                .agg(ads=('ad_id', 'count'),
                     first=('first_shown', 'min'),
                     last=('last_shown', 'max'))
                .reset_index().sort_values('ads', ascending=False).head(15))
        top = top.merge(latest_ad, on='handle', how='left')
        top['profile'] = top['handle'].apply(
            lambda h: f"https://www.tiktok.com/@{h}" if h and not str(h).isdigit() else "")
        top['latest_ad'] = top['_latest_ad_id'].apply(
            lambda a: f"https://library.tiktok.com/ads/detail/?ad_id={a}" if pd.notna(a) else "")
        top = top.drop(columns=['_latest_ad_id'])
        st.dataframe(top, use_container_width=True, hide_index=True,
                     column_config={
                         'profile':   st.column_config.LinkColumn('🔗 profile', display_text='Open profile'),
                         'latest_ad': st.column_config.LinkColumn('▶ latest ad', display_text='Open ad'),
                     })
    with c2:
        st.subheader("Reach distribution")
        reach_counts = f['reach_raw'].value_counts().head(10)
        st.bar_chart(reach_counts, height=300)

# ── Enforcement scorecard ────────────────────────────────────────────────────
# Single-page, journalist-quotable answer to: "How well does TikTok enforce
# its own no-political-ads policy in Cyprus?"
# Everything here is built from public data; the methodology is documented
# in-page so any number can be reproduced. The headline framing (the big
# percentage at the top + the 4 stat cards) is what a journalist or
# regulator can paste into a story or report.
with tab_enforce:
    st.subheader("🚨 TikTok enforcement scorecard — Cyprus 2026")
    st.caption(
        "TikTok bans paid political advertising globally. This page measures "
        "how well that ban is enforced in Cyprus, based on the ads we've "
        "discovered in TikTok's own Commercial Content Library + each ad's "
        "current status (active / inactive / removed by TikTok). "
        "Every number on this page is reproducible from the public DB; "
        "see the methodology note at the bottom."
    )

    # ─── Build the universe of "potential policy violations" ─────────
    # Definition: any ad in a political tier — candidates, supporters,
    # party accounts, party-aligned movements. Excludes commentators and
    # news outlets (which may be exempt or fall in a grey area).
    POLITICAL_TIERS = {'manual_resume', 'party_coordinator',
                       'political_movement', 'party_supporter', 'party_account'}
    pol = f[f['match_type'].isin(POLITICAL_TIERS)].copy()

    # Detect removal: derive_status() already handles the
    # 'inactive + status_statement contains removed/violation' pattern.
    pol['removed_by_tiktok'] = pol['derived_status'] == '🚨 Removed by TikTok'

    n_detected     = len(pol)
    n_removed      = int(pol['removed_by_tiktok'].sum())
    n_still_active = int(pol['ad_status'].fillna('').str.lower().eq('active').sum())
    n_advertisers  = pol['advertiser_id'].nunique()
    removal_rate   = (n_removed / n_detected * 100) if n_detected else 0.0

    # Spend exposed before takedown — uses the mid-CPM estimate
    if 'estimated_spend_eur_mid' in pol.columns:
        eur_removed       = int(pol[pol['removed_by_tiktok']]['estimated_spend_eur_mid'].fillna(0).sum())
        eur_still_active  = int(pol[pol['ad_status'].fillna('').str.lower().eq('active')]['estimated_spend_eur_mid'].fillna(0).sum())
        eur_total_political = int(pol['estimated_spend_eur_mid'].fillna(0).sum())
    else:
        eur_removed = eur_still_active = eur_total_political = 0

    # Median days to removal — for the K rows TikTok removed, how long
    # between when we first saw the ad live and when status_change_log
    # marked it removed?
    median_days_to_removal = None
    try:
        conn = sqlite3.connect(DB)
        # Find removal events in tiktok_ad_status_changes that match
        # our political-tier ad_ids, and pair with first_shown in tiktok_ads.
        ad_ids = ", ".join(f"'{a}'" for a in pol[pol['removed_by_tiktok']]['ad_id'].tolist())
        if ad_ids:
            days_rows = conn.execute(f"""
                SELECT
                  julianday(sc.observed_at) - julianday(a.first_shown) AS days_to_removal
                FROM tiktok_ad_status_changes sc
                JOIN tiktok_ads a USING(ad_id)
                WHERE sc.ad_id IN ({ad_ids})
                  AND (
                    LOWER(IFNULL(sc.new_statement,'')) LIKE '%removed%'
                    OR LOWER(IFNULL(sc.new_statement,'')) LIKE '%violation%'
                  )
            """).fetchall()
            if days_rows:
                values = sorted([float(r[0]) for r in days_rows if r[0] is not None])
                if values:
                    median_days_to_removal = values[len(values)//2]
        conn.close()
    except Exception:
        pass

    # ─── Headline metric: BIG removal rate ─────────────────────────────
    st.markdown(
        f"<div style='text-align:center; padding: 30px 20px 10px;'>"
        f"<div style='font-size: 18px; color:#666;'>TikTok enforcement rate (Cyprus 2026)</div>"
        f"<div style='font-size: 80px; font-weight: bold; color: "
        f"{'#d62728' if removal_rate < 50 else '#2ca02c'};'>{removal_rate:.1f}%</div>"
        f"<div style='font-size:16px; color:#666;'>"
        f"of detected political ads were removed by TikTok"
        f"</div></div>",
        unsafe_allow_html=True,
    )

    # ─── 4 stat cards ─────────────────────────────────────────────────
    st.divider()
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Political ads detected", f"{n_detected:,}",
              help="All ads in candidate/supporter/party tiers")
    k2.metric("Removed by TikTok", f"{n_removed:,}",
              help="Ad status reports 'removed' or 'violation' in status_statement")
    k3.metric("Still active",        f"{n_still_active:,}",
              help="ad_status='active' as of last refresh — ongoing policy violations")
    k4.metric("Median days to removal",
              f"{median_days_to_removal:.0f}" if median_days_to_removal else "—",
              help="Days between first_shown and the takedown event in status_changes log")

    # ─── Money exposed ────────────────────────────────────────────────
    st.divider()
    st.markdown("### 💶 Estimated spend exposed (before takedown)")
    e1, e2, e3 = st.columns(3)
    e1.metric("On removed ads",         f"€{eur_removed:,}",
              help="Mid-CPM estimate of spend on ads TikTok eventually removed")
    e2.metric("On still-active ads",    f"€{eur_still_active:,}",
              help="Mid-CPM estimate of currently-violating spend")
    e3.metric("Total detected",         f"€{eur_total_political:,}",
              help="Mid-CPM estimate across all political-tier ads")

    if eur_total_political > 0:
        enforcement_eur_pct = eur_removed / eur_total_political * 100
        st.caption(
            f"TikTok removed **€{eur_removed:,} of an estimated €{eur_total_political:,}** "
            f"in political ad spend — **{enforcement_eur_pct:.1f}%** by money "
            f"({removal_rate:.1f}% by ad count). "
            f"€{eur_still_active:,} of policy-violating spend is currently still live."
        )

    # ─── Trend: detected vs removed by month ──────────────────────────
    st.divider()
    st.markdown("### Monthly detection vs removal")
    if not pol.empty:
        pol_dt = pol.copy()
        pol_dt['month'] = pol_dt['first_shown'].dt.to_period('M').dt.to_timestamp()
        monthly = (pol_dt
                    .groupby('month', as_index=False)
                    .agg(detected=('ad_id', 'count'),
                         removed =('removed_by_tiktok', 'sum')))
        monthly['rate %'] = (monthly['removed'] / monthly['detected'] * 100).round(1)
        st.bar_chart(monthly.set_index('month')[['detected', 'removed']], height=280)
        st.caption(
            "Bars: detected (all political ads first shown that month) vs removed "
            "(TikTok takedowns). A widening gap = enforcement falling behind."
        )

    # ─── Currently-live offenders worth flagging ──────────────────────
    st.divider()
    st.markdown("### Currently-live ads worth flagging to TikTok")
    st.caption(
        "Political ads with `ad_status='active'` AND mid-CPM estimate ≥ €100. "
        "Use `export_bulk_report.py` to produce a TikTok-reporter-form CSV "
        "and submit these for enforcement review."
    )
    live = pol[
        pol['ad_status'].fillna('').str.lower().eq('active')
    ].copy()
    if 'estimated_spend_eur_mid' in live.columns:
        live = live[live['estimated_spend_eur_mid'].fillna(0) >= 100]
    live = live.sort_values('estimated_spend_eur_mid', ascending=False).head(25)
    if live.empty:
        st.info("No still-live ads above the €100 spend threshold.")
    else:
        live['profile_url'] = live['handle'].apply(
            lambda h: f"https://www.tiktok.com/@{h}" if h and not str(h).isdigit() else "")
        cols_to_show = ['handle', 'matched_candidate', 'matched_party',
                        'ad_id', 'first_shown', 'last_shown',
                        'estimated_spend_eur_mid', 'ad_url', 'profile_url']
        st.dataframe(
            live[cols_to_show],
            use_container_width=True, hide_index=True,
            column_config={
                'ad_url':                  st.column_config.LinkColumn('▶ ad library', display_text='Open'),
                'profile_url':             st.column_config.LinkColumn('👤 profile',   display_text='Open'),
                'estimated_spend_eur_mid': st.column_config.NumberColumn('€ mid', format='€%d'),
                'first_shown':             st.column_config.DateColumn(),
                'last_shown':              st.column_config.DateColumn(),
            }
        )

    # ─── Methodology ──────────────────────────────────────────────────
    st.divider()
    with st.expander("📐 Methodology — how every number above is computed", expanded=False):
        st.markdown("""
**Universe of ads ("political")** — every row in `tiktok_ads` with `match_type` in:
`manual_resume`, `party_coordinator`, `political_movement`, `party_supporter`, `party_account`.
Excluded: `commentator`, `news_outlet`, `podcast`, `satirist`, `politician_non_candidate`
(grey-zone or arguably-exempt content).

**Detected** — count of those rows after the sidebar filter.

**Removed by TikTok** — `derived_status == '🚨 Removed by TikTok'`. The derive function checks BOTH `ad_status` (sometimes flips to `removed_by_tiktok`) AND `status_statement` (often contains *"Removed from TikTok due to a violation of TikTok's terms"* even when `ad_status` stays `inactive`).

**Still active** — `ad_status='active'` per the most recent `refresh_ad_statuses.py` run. Stale if the daily cron hasn't fired; see the green badge at the top of the dashboard for last-refresh time.

**Median days to removal** — for each removed ad, `julianday(observed_at) - julianday(first_shown)` from `tiktok_ad_status_changes`, then the median.

**€ estimates** — `times_shown_lower/upper_bound × CPM / 1000`. CPMs: €3 (low) / €5 (mid) / €8 (high). Mid is reported here. Bounded by `--limit 30` per cron run on auto-review, so spend numbers might lag slightly for the freshest ads.

**Why this is not a perfect measure of enforcement** —
1. We only see what TikTok itself publishes in the Commercial Content Library; ads that were never indexed don't appear.
2. TikTok's reach buckets are wide (the 10K-100K bucket is a 10× range), so € estimates have meaningful precision limits.
3. "Removed by TikTok" includes only ads where TikTok publicly attributed removal to a policy violation; some are just inactive without explanation.

All code is open-source. To reproduce any number: clone the deploy repo, open the public DB in SQLite, and run the queries above.
        """)

# ── Spend (estimated €) ───────────────────────────────────────────────────────
# Aggregates the per-ad estimates into per-party / per-candidate /
# per-month views so the data tells a story instead of being 524 rows.
# Every chart on this page is built on the mid-CPM estimate (€5/k). The
# low/high range is reported on the banner at the top of the page.
with tab_spend:
    st.subheader("Estimated political-ad spend on TikTok in Cyprus")
    st.caption(
        f"All numbers below are **estimates** computed from TikTok's published "
        f"reach buckets × EU commercial CPM (€{3}/k low — €{5}/k mid — €{8}/k high). "
        f"TikTok bans paid political ads globally; reaches reported here are "
        f"likely policy violations regardless of who paid for them. The mid "
        f"figure is the default for ranking; bounds are shown to communicate "
        f"the precision limit (TikTok's reach buckets are wide — e.g. "
        f"'10K-100K' is a 10× range)."
    )

    if 'estimated_spend_eur_mid' not in f.columns or f['estimated_spend_eur_mid'].sum() == 0:
        st.warning(
            "No spend estimates in the DB. Run "
            "`python compute_spend_estimates.py` against this DB to populate them."
        )
    else:
        # Banner-level totals
        col_l, col_m, col_h = st.columns(3)
        col_l.metric("Conservative (low)",  f"€{_spend_sum('estimated_spend_eur_low'):,}",
                     help="Sums lower reach bound × €3 CPM. Defensible floor.")
        col_m.metric("Best estimate (mid)", f"€{_spend_sum('estimated_spend_eur_mid'):,}",
                     help="Sums mid reach × €5 CPM. Use for rankings.")
        col_h.metric("Upper bound (high)",  f"€{_spend_sum('estimated_spend_eur_high'):,}",
                     help="Sums upper reach bound × €8 CPM. Use to highlight outliers.")

        st.divider()

        # ─── Spend by party ───────────────────────────────────────────
        st.markdown("### Spend by party")
        real_party_mask = ~f['matched_party'].fillna('').str.startswith('[content-keyword') \
                         & f['matched_party'].fillna('').ne('')
        party_spend = (f[real_party_mask]
                        .groupby('matched_party', as_index=False)
                        .agg(total_eur=('estimated_spend_eur_mid', 'sum'),
                             low_eur  =('estimated_spend_eur_low',  'sum'),
                             high_eur =('estimated_spend_eur_high', 'sum'),
                             ads      =('ad_id', 'count'),
                             advertisers=('advertiser_id', 'nunique'))
                        .sort_values('total_eur', ascending=False))
        if not party_spend.empty:
            st.bar_chart(party_spend.set_index('matched_party')['total_eur'], height=320)
            party_spend.columns = ['Party', '€ mid', '€ low', '€ high', 'Ads', 'Advertisers']
            party_spend[['€ mid', '€ low', '€ high']] = party_spend[['€ mid', '€ low', '€ high']].astype(int)
            st.dataframe(party_spend, use_container_width=True, hide_index=True,
                         column_config={
                             '€ mid':  st.column_config.NumberColumn(format='€%d'),
                             '€ low':  st.column_config.NumberColumn(format='€%d'),
                             '€ high': st.column_config.NumberColumn(format='€%d'),
                         })

        st.divider()

        # ─── Top-spending candidates ──────────────────────────────────
        st.markdown("### Top spenders (per candidate)")
        cand_mask = f['matched_candidate'].fillna('').ne('') & \
                    f['match_type'].eq('manual_resume')
        cand_spend = (f[cand_mask]
                       .groupby(['matched_candidate', 'matched_party', 'matched_district', 'handle'],
                                as_index=False)
                       .agg(total_eur=('estimated_spend_eur_mid', 'sum'),
                            low_eur  =('estimated_spend_eur_low',  'sum'),
                            high_eur =('estimated_spend_eur_high', 'sum'),
                            ads      =('ad_id', 'count'))
                       .sort_values('total_eur', ascending=False)
                       .head(30))
        cand_spend['profile'] = cand_spend['handle'].apply(
            lambda h: f"https://www.tiktok.com/@{h}" if h and not str(h).isdigit() else "")
        st.dataframe(
            cand_spend[['matched_candidate', 'matched_party', 'matched_district',
                        'ads', 'total_eur', 'low_eur', 'high_eur', 'profile']],
            use_container_width=True, hide_index=True,
            column_config={
                'total_eur': st.column_config.NumberColumn('€ mid',  format='€%d'),
                'low_eur':   st.column_config.NumberColumn('€ low',  format='€%d'),
                'high_eur':  st.column_config.NumberColumn('€ high', format='€%d'),
                'profile':   st.column_config.LinkColumn('👤', display_text='Open'),
            })

        st.divider()

        # ─── Spend over time (weekly) ─────────────────────────────────
        st.markdown("### Estimated spend over time (weekly)")
        weekly = (f.set_index('first_shown')
                   .groupby(pd.Grouper(freq='W'))
                   .agg(eur=('estimated_spend_eur_mid', 'sum'),
                        ads=('ad_id', 'count'))
                   .reset_index())
        if not weekly.empty:
            weekly.columns = ['week', '€ mid spend', 'ads launched']
            st.line_chart(weekly.set_index('week')['€ mid spend'], height=280)
            st.caption("Each point = sum of mid-CPM estimates for ads first shown in that week. "
                       "Useful for spotting spend surges around debates, scandals, "
                       "or the election-silence window.")

# ── By party ──────────────────────────────────────────────────────────────────
with tab_party:
    st.subheader("Party-by-party ecosystem")
    real_party = f[~f['matched_party'].fillna('').str.startswith('[content-keyword')
                   & f['matched_party'].notna() & (f['matched_party'] != '')]

    # Per-party rollup with breakdown by category
    def per_party_breakdown(grp):
        return pd.Series({
            'ads':              len(grp),
            'advertisers':      grp['advertiser_id'].nunique(),
            'candidates':       grp[grp['match_type'] == 'manual_resume']['matched_candidate'].nunique(),
            'party_accounts':   grp[grp['match_type'].isin(['party_account','party_coordinator'])]['advertiser_id'].nunique(),
            'supporters':       grp[grp['match_type'] == 'party_supporter']['advertiser_id'].nunique(),
            'commentators':     grp[grp['match_type'].isin(['commentator','news_outlet','podcast','satirist'])]['advertiser_id'].nunique(),
        })
    party_stats = real_party.groupby('matched_party').apply(per_party_breakdown).reset_index()
    if not candidates_df.empty:
        roster = candidates_df.groupby('party').size().reset_index(name='roster_size')
        party_stats = party_stats.merge(roster, left_on='matched_party', right_on='party', how='left').drop(columns=['party'])
        party_stats['% roster with ads'] = (
            party_stats['candidates'] / party_stats['roster_size'].replace(0, np.nan) * 100).round(1)
    party_stats = party_stats.sort_values('ads', ascending=False)
    st.dataframe(party_stats, use_container_width=True, hide_index=True)

    if not party_stats.empty:
        st.bar_chart(party_stats.set_index('matched_party')['ads'], height=320)

    # ── Drill into one party — see candidates + party accounts + supporters ──
    st.divider()
    st.subheader("📂 Drill into one party")
    party_options = ['(pick a party)'] + party_stats['matched_party'].tolist()
    picked_party = st.selectbox("Pick a party to see its full TikTok presence", options=party_options)
    if picked_party != '(pick a party)':
        p_df = real_party[real_party['matched_party'] == picked_party]
        for label, cat_filter in [
            ("🟢 Candidates",        ['manual_resume']),
            ("🟣 Party accounts",    ['party_account', 'party_coordinator']),
            ("🟡 Supporters",        ['party_supporter']),
            ("🟠 Aligned media",     ['commentator', 'news_outlet', 'podcast', 'satirist']),
        ]:
            sub = p_df[p_df['match_type'].isin(cat_filter)]
            adv = (sub.drop_duplicates('advertiser_id')
                       .groupby(['handle', 'matched_candidate', 'matched_district'], dropna=False)
                       .agg(ads=('ad_id', 'count'),
                            last=('last_shown', 'max'))
                       .reset_index().sort_values('ads', ascending=False))
            if adv.empty: continue
            st.write(f"**{label} — {len(adv)} accounts**")
            adv['profile'] = adv['handle'].apply(
                lambda h: f"https://www.tiktok.com/@{h}" if h and not str(h).isdigit() else "")
            st.dataframe(adv, use_container_width=True, hide_index=True,
                         column_config={
                             'profile': st.column_config.LinkColumn('🔗 profile', display_text='Open profile'),
                         })

# ── By candidate ──────────────────────────────────────────────────────────────
with tab_candidates:
    # Most-recent ad per candidate, for deep-link button
    latest_per_cand = (f[f['matched_candidate'] != '']
                        .sort_values('last_shown', ascending=False)
                        .drop_duplicates(['matched_candidate', 'handle'])
                        [['matched_candidate', 'handle', 'ad_id']]
                        .rename(columns={'ad_id': '_latest_ad_id'}))
    cand_stats = (f[f['matched_candidate'] != '']
                  .groupby(['matched_candidate', 'matched_party', 'matched_district', 'handle'])
                  .agg(ads=('ad_id', 'count'),
                       first=('first_shown', 'min'),
                       last=('last_shown', 'max'),
                       active=('ad_status', lambda s: (s == 'active').sum()))
                  .reset_index().sort_values('ads', ascending=False))
    cand_stats = cand_stats.merge(latest_per_cand, on=['matched_candidate', 'handle'], how='left')
    cand_stats['profile'] = cand_stats['handle'].apply(
        lambda h: f"https://www.tiktok.com/@{h}" if h and not str(h).isdigit() else "")
    cand_stats['latest_ad'] = cand_stats['_latest_ad_id'].apply(
        lambda a: f"https://library.tiktok.com/ads/detail/?ad_id={a}" if pd.notna(a) else "")
    cand_stats = cand_stats.drop(columns=['_latest_ad_id'])

    st.subheader("Candidates with TikTok ads")
    st.dataframe(cand_stats, use_container_width=True, hide_index=True,
                 column_config={
                     'profile':   st.column_config.LinkColumn('🔗 profile', display_text='Open profile'),
                     'latest_ad': st.column_config.LinkColumn('▶ latest ad', display_text='Open ad'),
                 })

    st.divider()
    st.subheader("📂 See ALL ads from a candidate")
    if not cand_stats.empty:
        # Build labelled options like "Παρασχού Αντώνης — ΣΗΚΟΥ ΠΑΝΩ · Αμμόχωστος (3 ads)"
        opts = cand_stats.assign(
            label=lambda d: d.apply(
                lambda r: f"{r['matched_candidate']} — {r['matched_party']} · {r['matched_district']}  ({r['ads']} ads)",
                axis=1)
        )[['label', 'handle']].drop_duplicates('label')

        picked_label = st.selectbox(
            "Pick a candidate to see every one of their ads",
            options=opts['label'].tolist(),
            index=None,
            placeholder="Type to search candidate name, party, or district…",
        )
        if picked_label:
            picked_handle = opts.loc[opts['label'] == picked_label, 'handle'].iloc[0]
            cand_ads = f[f['handle'] == picked_handle].sort_values('first_shown', ascending=False)
            prof_url = f"https://www.tiktok.com/@{picked_handle}" if picked_handle and not str(picked_handle).isdigit() else ""
            cand_row = cand_stats[cand_stats['handle'] == picked_handle].iloc[0]

            # Header card
            h1, h2 = st.columns([3, 2])
            with h1:
                st.markdown(f"### {cand_row['matched_candidate']}")
                st.markdown(f"**{cand_row['matched_party']}** · {cand_row['matched_district']}  ·  `@{picked_handle}`")
                st.caption(f"{cand_row['ads']} ads · first {cand_row['first'].date() if pd.notna(cand_row['first']) else '?'} · last {cand_row['last'].date() if pd.notna(cand_row['last']) else '?'} · {cand_row['active']} currently active")
            with h2:
                if prof_url:
                    st.link_button(f"🔗 Open @{picked_handle} on TikTok",
                                    prof_url, use_container_width=True)

            # All ads, most recent first
            st.markdown(f"##### All {len(cand_ads)} ads")
            for _, ad in cand_ads.iterrows():
                date_str = (f"{ad['first_shown'].date()} → {ad['last_shown'].date()}"
                            if pd.notna(ad['first_shown']) and pd.notna(ad['last_shown']) else "?")
                with st.expander(f"📺 {date_str}  ·  {ad['kind']}  ·  reach {ad['reach_raw']}  ·  ad_id {ad['ad_id']}"):
                    cA, cB = st.columns([3, 2])
                    with cA:
                        st.info("🎬 Click below to view the ad on TikTok's official Ad Library")
                        bb1, bb2 = st.columns(2)
                        with bb1:
                            st.link_button("▶ View ad", ad['library_url'], use_container_width=True)
                        with bb2:
                            if prof_url:
                                st.link_button("🔗 Profile", prof_url, use_container_width=True)
                    with cB:
                        st.write(f"**Status:** {ad['ad_status']}")
                        st.write(f"**Days active:** {ad['days_active']}")
                        st.write(f"**Reach:** {ad['reach_raw']}")
                        transcript = ad['transcript'] if pd.notna(ad.get('transcript')) else ''
                        if isinstance(transcript, str) and len(transcript) > 20:
                            with st.expander("📝 Transcript"):
                                st.text(transcript)
    else:
        st.info("No candidate ads match the current filters. Loosen the sidebar filters to see more.")

# ── Status & changes ──────────────────────────────────────────────────────────
with tab_status:
    st.subheader("Ad lifecycle — active vs inactive vs removed")
    st.caption(
        "Statuses below combine TikTok's reported ad_status (refreshed by "
        "`refresh_ad_statuses.py`) with date-derived fallbacks. Until the "
        "status refresh has been run, ads default to ✅ Active if shown in "
        "the last 7 days."
    )

    # KPI row
    status_counts = f['derived_status'].value_counts()
    status_order = [
        '🚨 Removed by TikTok',
        '🗑 Deleted by advertiser',
        '⌛ Expired',
        '✅ Active (last 7 days)',
        '🟡 Recently inactive (8–30 days)',
        '⚪ Dormant (30+ days)',
        '❓ Unknown',
    ]
    kpi_cols = st.columns(min(4, len(status_order)))
    for i, label in enumerate(status_order[:4]):
        with kpi_cols[i]:
            st.metric(label, int(status_counts.get(label, 0)))

    st.bar_chart(status_counts.reindex(status_order).dropna(), height=280)

    # Status filter — drill into one bucket
    st.divider()
    pick_status = st.selectbox(
        "Show ads in status",
        options=['(all)'] + status_order,
        index=1,   # default to 'Removed by TikTok' for newsworthy stuff
    )
    status_filtered = f if pick_status == '(all)' else f[f['derived_status'] == pick_status]
    st.write(f"**{len(status_filtered)} ads** in `{pick_status}`")
    if not status_filtered.empty:
        cols = ['derived_status', 'handle', 'matched_candidate', 'matched_party',
                'matched_district', 'first_shown', 'last_shown', 'reach_raw',
                'ad_url', 'profile_url']
        st.dataframe(status_filtered[cols].sort_values('last_shown', ascending=False),
                     use_container_width=True, hide_index=True,
                     column_config={
                         'ad_url':      st.column_config.LinkColumn('▶ ad', display_text='Open ad'),
                         'profile_url': st.column_config.LinkColumn('🔗 profile', display_text='Open profile'),
                         'first_shown': st.column_config.DateColumn(),
                         'last_shown':  st.column_config.DateColumn(),
                     })

    # ─── Status-change history (from tiktok_ad_status_changes table) ───
    st.divider()
    st.subheader("📜 Status-change history")
    try:
        conn = sqlite3.connect(DB)
        changes = pd.read_sql_query("""
            SELECT sc.observed_at, sc.ad_id, sc.prev_status, sc.new_status,
                   sc.new_statement, sc.handle,
                   a.matched_candidate, a.matched_party, a.matched_district,
                   a.ad_url, a.videos_json, a.image_urls_json
            FROM tiktok_ad_status_changes sc
            LEFT JOIN tiktok_ads a USING(ad_id)
            ORDER BY sc.observed_at DESC
            LIMIT 500
        """, conn)
        conn.close()
        changes_loaded = True
        # Derive profile URL and CDN URL (first video / first image)
        def _profile(h):
            if not h or str(h).isdigit(): return ''
            return f"https://www.tiktok.com/@{h}"
        def _cdn(row):
            try:
                vids = json.loads(row.get('videos_json') or '[]')
                if vids:
                    return vids[0].get('url') or ''
                imgs = json.loads(row.get('image_urls_json') or '[]')
                if imgs:
                    first = imgs[0]
                    return first if isinstance(first, str) else (first.get('url', '') if isinstance(first, dict) else '')
            except Exception:
                pass
            return ''
        if not changes.empty:
            changes['profile_url'] = changes['handle'].apply(_profile)
            changes['cdn_url'] = changes.apply(_cdn, axis=1)
    except Exception:
        changes = pd.DataFrame()
        changes_loaded = False

    if not changes_loaded or changes.empty:
        st.info(
            "No status-change history yet. Run `python refresh_ad_statuses.py` "
            "to query TikTok's `/v2/research/adlib/ad/detail/` endpoint for each "
            "ad and start populating this log. Subsequent runs will detect "
            "transitions (`active` → `removed_by_tiktok`, etc.) and record them here."
        )
    else:
        st.write(f"**{len(changes)} most-recent transitions** (newest first)")
        changes['observed_at'] = pd.to_datetime(changes['observed_at'])
        changes['transition'] = changes['prev_status'].fillna('?') + ' → ' + changes['new_status'].fillna('?')
        st.dataframe(
            changes[['observed_at', 'transition', 'handle', 'matched_candidate',
                     'matched_party', 'new_statement', 'ad_url',
                     'profile_url', 'cdn_url']],
            use_container_width=True, hide_index=True,
            column_config={
                'observed_at':   st.column_config.DatetimeColumn('When (UTC)'),
                'ad_url':        st.column_config.LinkColumn('▶ ad library', display_text='Open in library'),
                'profile_url':   st.column_config.LinkColumn('👤 profile', display_text='Open profile'),
                'cdn_url':       st.column_config.LinkColumn('🎬 creative', display_text='Open creative'),
                'new_statement': st.column_config.Column('TikTok reason', width='large'),
            }
        )

        # Summary by transition type
        st.divider()
        cA, cB = st.columns(2)
        with cA:
            st.subheader("Transitions by type")
            st.bar_chart(changes['transition'].value_counts().head(10), height=300)
        with cB:
            st.subheader("Changes over time (weekly)")
            timeline = (changes.set_index('observed_at')
                              .groupby(pd.Grouper(freq='W'))
                              .size().reset_index(name='changes'))
            timeline.columns = ['week', 'changes']
            st.line_chart(timeline, x='week', y='changes', height=300)

        # Headline: removed by TikTok — match on either new_status or the
        # violation note in new_statement (TikTok often leaves status as
        # 'inactive' but puts the takedown reason in new_statement).
        stmt_lower = changes['new_statement'].fillna('').str.lower()
        is_removed = (changes['new_status'] == 'removed_by_tiktok') | \
                     stmt_lower.str.contains('removed', na=False) | \
                     stmt_lower.str.contains('violation', na=False)
        removed = changes[is_removed]
        if not removed.empty:
            st.divider()
            st.subheader(f"🚨 {len(removed)} ads have been REMOVED by TikTok")
            st.dataframe(removed[['observed_at', 'handle', 'matched_candidate',
                                  'matched_party', 'matched_district',
                                  'new_statement', 'ad_url', 'profile_url', 'cdn_url']],
                         use_container_width=True, hide_index=True,
                         column_config={
                             'observed_at':   st.column_config.DatetimeColumn(),
                             'ad_url':        st.column_config.LinkColumn('▶ ad library', display_text='Open in library'),
                             'profile_url':   st.column_config.LinkColumn('👤 profile', display_text='Open profile'),
                             'cdn_url':       st.column_config.LinkColumn('🎬 creative', display_text='Open creative'),
                             'new_statement': st.column_config.Column('TikTok reason', width='large'),
                         })

# ── Review queue ──────────────────────────────────────────────────────────────
# Surfaces handles that warrant a human second-opinion check, because each
# of them belongs to the bug class we hit repeatedly today:
#   - @petrouiakovos / @champis_me_p / @ttoppouzi / @marioshaperis were all
#     classified as candidates/relevant, but turned out to be false
#     positives the user caught only by chance.
#   - High-reach political-tier ads with NO transcript yet are exactly the
#     ones a reviewer should spot-check.
#
# Three sections, lightest first:
#   A. "Promoted recently" — anything moved into a political tier in the
#      last 14 days. Even one wrong promotion has outsized impact.
#   B. "Lower-confidence + high reach" — commentator/podcast/satirist/etc.
#      with at least one ad in the 10K-100K bucket or higher. Cheap signals
#      we might've misclassified.
#   C. "Promoted but never transcribed" — manual_resume handles whose ads
#      have no Whisper transcript yet. Often means we're trusting the
#      handle match without ever looking at the actual content.
with tab_review:
    st.subheader("🔍 Handles worth a second look")
    st.caption(
        "Three review buckets, each surfacing a different class of "
        "potential false-positive. Use `python promote.py` to confirm or "
        "`python flag.py` to demote, then rebuild the public DB."
    )

    # Shared: per-handle aggregate that's useful in every section
    handles = (f.groupby('handle', as_index=False)
                .agg(matched_candidate=('matched_candidate', 'first'),
                     matched_party    =('matched_party',     'first'),
                     matched_district =('matched_district',  'first'),
                     match_type       =('match_type',        'first'),
                     ads              =('ad_id', 'count'),
                     max_reach_upper  =('times_shown_upper_bound', 'max'),
                     first_seen       =('first_shown',       'min'),
                     last_seen        =('last_shown',        'max'),
                     has_transcript   =('transcript', lambda s: s.notna().any()),
                     auto_verdict     =('auto_review_verdict',    'first'),
                     auto_confidence  =('auto_review_confidence', 'first'),
                     auto_reason      =('auto_review_reason',     'first')))
    handles['profile_url'] = handles['handle'].apply(
        lambda h: f"https://www.tiktok.com/@{h}" if h and not str(h).isdigit() else "")

    # Compute auto-review disagreement flag — Claude's verdict vs our current tier
    AGREE_MAP = {
        'candidate':     {'manual_resume'},
        'supporter':     {'party_supporter'},
        'commentator':   {'commentator', 'satirist'},
        'party_account': {'party_account', 'party_coordinator', 'political_movement'},
        'news_outlet':   {'news_outlet', 'podcast'},
        'fp_business':   {'likely_false_positive_business'},
        'fp_personal':   {'likely_false_positive_personal'},
    }
    def _disagrees(row):
        v = row['auto_verdict']
        if v is None or pd.isna(v) or v == 'unclear':
            return False
        return row['match_type'] not in AGREE_MAP.get(v, set())
    handles['auto_disagrees'] = handles.apply(_disagrees, axis=1)

    LOWER_CONFIDENCE_TIERS = {
        'commentator', 'podcast', 'satirist', 'news_outlet',
        'politician_non_candidate', 'party_supporter', 'party_account',
    }
    REVIEW_COLS = ['handle', 'match_type', 'auto_verdict', 'auto_confidence',
                   'auto_reason', 'matched_candidate', 'matched_party',
                   'matched_district', 'ads', 'max_reach_upper',
                   'first_seen', 'last_seen', 'profile_url']
    REVIEW_COL_CFG = {
        'profile_url':     st.column_config.LinkColumn('👤 profile', display_text='Open'),
        'max_reach_upper': st.column_config.NumberColumn('reach ≥', format='%d'),
        'first_seen':      st.column_config.DateColumn(),
        'last_seen':       st.column_config.DateColumn(),
        'auto_verdict':    st.column_config.Column('🤖 verdict', width='small'),
        'auto_confidence': st.column_config.NumberColumn('conf', format='%.2f'),
        'auto_reason':     st.column_config.Column('🤖 reason', width='large'),
    }

    # ─── A0. Claude disagrees with our classification ─────────────────
    st.divider()
    st.markdown("### 🤖 Auto-review disagreements (highest priority)")
    st.caption(
        "Every handle where Claude's auto_review verdict (run via "
        "`python auto_review.py`) doesn't match our current `match_type`. "
        "These are the strongest single signal we have for a "
        "misclassification — closest to a true second opinion."
    )
    disagreements = handles[handles['auto_disagrees']].sort_values(
        'auto_confidence', ascending=False)
    if disagreements.empty:
        st.info("No auto-review disagreements (either nothing reviewed yet, "
                "or every reviewed handle matches our classification).")
    else:
        st.write(f"**{len(disagreements)} handle(s)** where Claude disagrees:")
        st.dataframe(disagreements[REVIEW_COLS],
                     use_container_width=True, hide_index=True,
                     column_config=REVIEW_COL_CFG)

    # ─── A. Promoted recently ──────────────────────────────────────────
    st.divider()
    st.markdown("### A. Promoted recently (last 14 days)")
    st.caption(
        "Anything moved into a political-content tier (anything except "
        "`content_keyword`/`likely_false_positive_*`) within the last 14 "
        "days. New promotions are the easiest place for a false positive "
        "to slip through."
    )
    cutoff = pd.Timestamp.today() - pd.Timedelta(days=14)
    recent_promotions = handles[
        (handles['match_type'] != 'content_keyword') &
        ~handles['match_type'].fillna('').str.startswith('likely_false_positive') &
        (handles['last_seen'] >= cutoff)
    ].sort_values('last_seen', ascending=False)
    if recent_promotions.empty:
        st.info("No promotions in the last 14 days.")
    else:
        st.write(f"**{len(recent_promotions)} handles** to spot-check:")
        st.dataframe(recent_promotions[REVIEW_COLS],
                     use_container_width=True, hide_index=True,
                     column_config=REVIEW_COL_CFG)

    # ─── B. Lower-confidence tier + high reach ────────────────────────
    st.divider()
    st.markdown("### B. Lower-confidence tier with high reach")
    st.caption(
        "Handles classified as commentator/podcast/satirist/news_outlet/"
        "supporter/party_account etc. AND running at least one ad in the "
        "10K-100K bucket or higher. If a misclassified business slipped "
        "in here it'd be reaching tens of thousands of people."
    )
    suspect = handles[
        handles['match_type'].isin(LOWER_CONFIDENCE_TIERS) &
        (handles['max_reach_upper'] >= 10_000)
    ].sort_values('max_reach_upper', ascending=False)
    if suspect.empty:
        st.info("Nothing in lower-confidence tiers with reach ≥ 10K.")
    else:
        st.write(f"**{len(suspect)} handles** with high reach in lower-confidence tiers:")
        st.dataframe(suspect[REVIEW_COLS],
                     use_container_width=True, hide_index=True,
                     column_config=REVIEW_COL_CFG)

    # ─── C. Promoted but never transcribed ────────────────────────────
    st.divider()
    st.markdown("### C. Confirmed candidates without any transcript")
    st.caption(
        "`manual_resume` handles whose ads have never been transcribed by "
        "Whisper. Without a transcript we're trusting the handle-to-"
        "candidate name match without seeing the actual ad content. "
        "Run `python transcribe_tiktok_creatives.py` to fill these in."
    )
    untranscribed = handles[
        (handles['match_type'] == 'manual_resume') &
        (~handles['has_transcript'])
    ].sort_values('ads', ascending=False)
    if untranscribed.empty:
        st.info("Every confirmed candidate has at least one transcribed ad.")
    else:
        st.write(f"**{len(untranscribed)} confirmed candidates** with zero transcripts:")
        st.dataframe(untranscribed[REVIEW_COLS],
                     use_container_width=True, hide_index=True,
                     column_config=REVIEW_COL_CFG)

# ── Browse individual ads ─────────────────────────────────────────────────────
with tab_browse:
    # Aggregate per handle so the selectbox always has exactly one label per
    # handle — if a handle has rows with mixed candidate/party values
    # (e.g. after a partial refresh), the previous .drop_duplicates() left
    # multiple rows for the same handle and .loc[h] returned a Series, which
    # crashes st.selectbox with a TypeError.
    def _label(row):
        cand = (row['matched_candidate'] or '').strip()
        party = (row['matched_party'] or '').strip()
        if cand:
            return f"@{row['handle']} → {cand} ({party})" if party else f"@{row['handle']} → {cand}"
        return f"@{row['handle']}"

    advertisers_with_ads = (f[['handle', 'matched_candidate', 'matched_party']]
                             .fillna('')
                             .groupby('handle', as_index=False)
                             .first())
    advertisers_with_ads['label'] = advertisers_with_ads.apply(_label, axis=1)
    advertisers_with_ads = advertisers_with_ads.sort_values('label')
    _label_lookup = dict(zip(advertisers_with_ads['handle'], advertisers_with_ads['label']))
    selected_handle = st.selectbox(
        "Pick an advertiser",
        options=advertisers_with_ads['handle'].tolist(),
        format_func=lambda h: _label_lookup.get(h, h),
    )
    if selected_handle:
        ads = f[f['handle'] == selected_handle].sort_values('first_shown')
        prof_url = f"https://www.tiktok.com/@{selected_handle}" if selected_handle and not str(selected_handle).isdigit() else ""
        head_c1, head_c2 = st.columns([3, 2])
        with head_c1:
            st.write(f"**{len(ads)} ads** for `@{selected_handle}`")
        with head_c2:
            if prof_url:
                st.link_button(f"🔗 Open @{selected_handle} on TikTok", prof_url, use_container_width=True)
        for _, ad in ads.iterrows():
            with st.expander(f"ad_id {ad['ad_id']} — {ad['first_shown'].date() if pd.notna(ad['first_shown']) else '?'} → {ad['last_shown'].date() if pd.notna(ad['last_shown']) else '?'}  ·  {ad['kind']}  ·  reach {ad['reach_raw']}"):
                cA, cB = st.columns([3, 2])
                with cA:
                    # Try to play from local file first (dev only — Streamlit Cloud won't have these)
                    local_dir = os.path.join(CREATIVES, ad['handle']) if ad['handle'] else ''
                    found_file = None
                    if local_dir and os.path.isdir(local_dir):
                        for fn in os.listdir(local_dir):
                            if fn.startswith(ad['ad_id']):
                                found_file = os.path.join(local_dir, fn)
                                break
                    if found_file and found_file.endswith('.mp4'):
                        st.video(found_file)
                    elif found_file and found_file.endswith('.jpg'):
                        st.image(found_file)
                    else:
                        st.info("🎬 Ad creative not bundled with the public snapshot — click below to view on TikTok Ad Library")

                    # Prominent link buttons (Streamlit's st.link_button renders as a real button)
                    btn_c1, btn_c2 = st.columns(2)
                    with btn_c1:
                        st.link_button("▶ View ad on TikTok Library", ad['library_url'], use_container_width=True)
                    with btn_c2:
                        if prof_url:
                            st.link_button(f"🔗 @{ad['handle']} profile", prof_url, use_container_width=True)
                with cB:
                    st.write(f"**Status:** {ad['ad_status']}")
                    st.write(f"**Days active:** {ad['days_active']}")
                    st.write(f"**Reach bucket:** {ad['reach_raw']}")
                    # Use pd.notna() — pandas NaN is truthy but breaks len()/string ops
                    cand = ad['matched_candidate'] if pd.notna(ad.get('matched_candidate')) else ''
                    if cand:
                        st.write(f"**Candidate:** {cand}")
                        party = ad['matched_party'] if pd.notna(ad.get('matched_party')) else ''
                        district = ad['matched_district'] if pd.notna(ad.get('matched_district')) else ''
                        if party:    st.write(f"**Party:** {party}")
                        if district: st.write(f"**District:** {district}")
                    transcript = ad['transcript'] if pd.notna(ad.get('transcript')) else ''
                    if isinstance(transcript, str) and len(transcript) > 20:
                        st.write("**Transcript:**")
                        st.text_area("", transcript, height=200,
                                     key=f"tx_{ad['ad_id']}", label_visibility='collapsed')

# ── Transcript search ─────────────────────────────────────────────────────────
with tab_transcripts:
    q = st.text_input("Search transcripts (case-insensitive, Greek or Latin)")
    if q:
        mask = f['transcript'].fillna('').str.contains(q, case=False, regex=False)
        hits = f[mask]
        st.write(f"**{len(hits)} ads** mention `{q}`")
        for _, ad in hits.head(50).iterrows():
            cand_label = ad['matched_candidate'] if pd.notna(ad.get('matched_candidate')) and ad.get('matched_candidate') else '(no candidate)'
            with st.expander(f"@{ad['handle']}  ·  {cand_label}  ·  {ad['first_shown'].date() if pd.notna(ad['first_shown']) else '?'}"):
                # Show snippet around the match
                txt = ad['transcript'] if pd.notna(ad.get('transcript')) else ''
                if not isinstance(txt, str): txt = ''
                idx = txt.lower().find(q.lower())
                if idx >= 0:
                    start = max(0, idx - 80)
                    end   = min(len(txt), idx + len(q) + 200)
                    st.markdown(f"...{txt[start:idx]}**{txt[idx:idx+len(q)]}**{txt[idx+len(q):end]}...")
                lc1, lc2 = st.columns(2)
                with lc1:
                    st.link_button("▶ View ad on TikTok Library", ad['library_url'], use_container_width=True)
                with lc2:
                    prof = f"https://www.tiktok.com/@{ad['handle']}" if ad['handle'] and not str(ad['handle']).isdigit() else ""
                    if prof:
                        st.link_button(f"🔗 @{ad['handle']} profile", prof, use_container_width=True)
    else:
        st.info("Type a word or phrase to search ad transcripts. Useful queries: party names (ΑΚΕΛ, ΔΗΣΥ, ΕΛΑΜ), policy terms (εκποίηση, στέγη, ψηφίστε), candidate names.")

# ── Raw data ──────────────────────────────────────────────────────────────────
with tab_raw:
    st.subheader(f"Total ads: {len(f)}")
    show_cols = ['match_type', 'handle', 'matched_candidate', 'matched_party',
                 'matched_district', 'ad_id', 'first_shown', 'last_shown',
                 'ad_status', 'reach_raw', 'kind', 'profile_url', 'library_url']
    st.dataframe(f[show_cols], use_container_width=True, hide_index=True,
                 column_config={
                     'profile_url': st.column_config.LinkColumn('🔗 profile', display_text='Open profile'),
                     'library_url': st.column_config.LinkColumn('▶ ad library', display_text='Open ad'),
                 })
    csv = f.to_csv(index=False).encode('utf-8')
    st.download_button("📥 Download CSV", csv,
                       file_name=f"tiktok_ads_{date.today()}.csv",
                       mime="text/csv")
