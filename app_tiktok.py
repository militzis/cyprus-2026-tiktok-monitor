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
    df = pd.read_sql_query("""
        SELECT advertiser_id, advertiser_disclosed_name AS handle,
               matched_candidate, matched_party, matched_district,
               ad_id, first_shown, last_shown, ad_status, reach_raw,
               times_shown_lower_bound, times_shown_upper_bound,
               ad_funded_by, videos_json, image_urls_json,
               ad_url, transcript, match_type, checked_at
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
st.caption(f"Last DB write: {df['checked_at'].max() if 'checked_at' in df.columns else '?'}  ·  "
           f"DB: `{DB}`")

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
    raw = (row.get('ad_status') or '').lower()
    if 'removed' in raw or 'violation' in raw:
        return '🚨 Removed by TikTok'
    if 'deleted' in raw:
        return '🗑 Deleted by advertiser'
    if raw == 'expired':
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
tab_overview, tab_party, tab_candidates, tab_status, tab_browse, tab_transcripts, tab_raw = st.tabs([
    "📊 Overview", "🏛 By party", "👤 By candidate", "🚦 Status & changes",
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
                        if ad.get('transcript') and len(ad['transcript']) > 20:
                            with st.expander("📝 Transcript"):
                                st.text(ad['transcript'])
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
                   a.matched_candidate, a.matched_party, a.matched_district, a.ad_url
            FROM tiktok_ad_status_changes sc
            LEFT JOIN tiktok_ads a USING(ad_id)
            ORDER BY sc.observed_at DESC
            LIMIT 500
        """, conn)
        conn.close()
        changes_loaded = True
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
                     'matched_party', 'new_statement', 'ad_url']],
            use_container_width=True, hide_index=True,
            column_config={
                'observed_at': st.column_config.DatetimeColumn('When (UTC)'),
                'ad_url':      st.column_config.LinkColumn('▶ ad', display_text='Open ad'),
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

        # Headline: removed by TikTok
        removed = changes[changes['new_status'] == 'removed_by_tiktok']
        if not removed.empty:
            st.divider()
            st.subheader(f"🚨 {len(removed)} ads have been REMOVED by TikTok")
            st.dataframe(removed[['observed_at', 'handle', 'matched_candidate',
                                  'matched_party', 'matched_district',
                                  'new_statement', 'ad_url']],
                         use_container_width=True, hide_index=True,
                         column_config={
                             'observed_at': st.column_config.DatetimeColumn(),
                             'ad_url': st.column_config.LinkColumn('▶ ad', display_text='Open ad'),
                         })

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
                    if ad.get('matched_candidate'):
                        st.write(f"**Candidate:** {ad['matched_candidate']}")
                        st.write(f"**Party:** {ad['matched_party']}")
                        st.write(f"**District:** {ad['matched_district']}")
                    if ad.get('transcript') and len(ad['transcript']) > 20:
                        st.write("**Transcript:**")
                        st.text_area("", ad['transcript'], height=200,
                                     key=f"tx_{ad['ad_id']}", label_visibility='collapsed')

# ── Transcript search ─────────────────────────────────────────────────────────
with tab_transcripts:
    q = st.text_input("Search transcripts (case-insensitive, Greek or Latin)")
    if q:
        mask = f['transcript'].fillna('').str.contains(q, case=False, regex=False)
        hits = f[mask]
        st.write(f"**{len(hits)} ads** mention `{q}`")
        for _, ad in hits.head(50).iterrows():
            with st.expander(f"@{ad['handle']}  ·  {ad['matched_candidate'] or '(no candidate)'}  ·  {ad['first_shown'].date() if pd.notna(ad['first_shown']) else '?'}"):
                # Show snippet around the match
                txt = ad['transcript'] or ''
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
