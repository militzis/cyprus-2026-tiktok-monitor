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
DB = os.environ.get('POLITICIAN_ADS_DB',
                    r'C:\Users\milit\meta_pipeline_data\politician_ads.db')
CREATIVES = os.environ.get('TIKTOK_CREATIVES_DIR',
                           r'C:\Users\milit\meta_pipeline_data\creatives')
CANDIDATES_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'candidates.csv')

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

all_match_types = sorted(df['match_type'].dropna().unique().tolist())
default_match = [m for m in all_match_types if m == 'manual_resume']
selected_match = st.sidebar.multiselect(
    "Match tier", all_match_types, default=default_match,
    help="manual_resume = confirmed political candidate. content_keyword* = found via keyword search, may include news/podcasts/satire.",
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
c1, c2, c3, c4 = st.columns(4)
c1.metric("Filtered ads", len(f))
c2.metric("Unique advertisers", f['advertiser_id'].nunique())
c3.metric("Active right now", (f['ad_status'] == 'active').sum())
c4.metric("Unique candidates matched", f[f['matched_candidate'] != '']['matched_candidate'].nunique())

st.divider()

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_overview, tab_party, tab_candidates, tab_browse, tab_transcripts, tab_raw = st.tabs([
    "📊 Overview", "🏛 By party", "👤 By candidate", "🎬 Browse ads", "📝 Transcript search", "🗂 Raw data",
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
        top = (f.groupby(['handle', 'matched_candidate', 'matched_party'])
                .agg(ads=('ad_id', 'count'),
                     first=('first_shown', 'min'),
                     last=('last_shown', 'max'))
                .reset_index().sort_values('ads', ascending=False).head(15))
        top['profile'] = top['handle'].apply(lambda h: f"https://www.tiktok.com/@{h}" if h and not str(h).isdigit() else "")
        st.dataframe(top, use_container_width=True, hide_index=True,
                     column_config={'profile': st.column_config.LinkColumn('profile')})
    with c2:
        st.subheader("Reach distribution")
        reach_counts = f['reach_raw'].value_counts().head(10)
        st.bar_chart(reach_counts, height=300)

# ── By party ──────────────────────────────────────────────────────────────────
with tab_party:
    st.subheader("Party coverage")
    # only count rows where matched_party is a real party (not the content-keyword placeholder)
    real_party = f[~f['matched_party'].fillna('').str.startswith('[content-keyword')]
    party_stats = (real_party.groupby('matched_party')
                   .agg(advertisers=('advertiser_id', 'nunique'),
                        ads=('ad_id', 'count'),
                        candidates=('matched_candidate', lambda s: s.nunique() if s.any() else 0))
                   .reset_index().sort_values('ads', ascending=False))
    if not candidates_df.empty:
        roster = candidates_df.groupby('party').size().reset_index(name='roster_size')
        party_stats = party_stats.merge(roster, left_on='matched_party', right_on='party', how='left').drop(columns=['party'])
        party_stats['% with TikTok ads'] = (
            party_stats['candidates'] / party_stats['roster_size'] * 100).round(1)
    st.dataframe(party_stats, use_container_width=True, hide_index=True)
    if not party_stats.empty:
        st.bar_chart(party_stats.set_index('matched_party')['ads'], height=320)

# ── By candidate ──────────────────────────────────────────────────────────────
with tab_candidates:
    cand_stats = (f[f['matched_candidate'] != '']
                  .groupby(['matched_candidate', 'matched_party', 'matched_district', 'handle'])
                  .agg(ads=('ad_id', 'count'),
                       first=('first_shown', 'min'),
                       last=('last_shown', 'max'),
                       active=('ad_status', lambda s: (s == 'active').sum()))
                  .reset_index().sort_values('ads', ascending=False))
    cand_stats['profile'] = cand_stats['handle'].apply(
        lambda h: f"https://www.tiktok.com/@{h}" if h and not str(h).isdigit() else "")
    st.dataframe(cand_stats, use_container_width=True, hide_index=True,
                 column_config={'profile': st.column_config.LinkColumn('profile')})

# ── Browse individual ads ─────────────────────────────────────────────────────
with tab_browse:
    advertisers_with_ads = (f[['handle', 'matched_candidate', 'matched_party']]
                             .drop_duplicates().fillna('')
                             .assign(label=lambda d: d.apply(
                                 lambda r: f"@{r['handle']} → {r['matched_candidate']} ({r['matched_party']})"
                                 if r['matched_candidate'] else f"@{r['handle']}", axis=1))
                             .sort_values('label'))
    selected_handle = st.selectbox("Pick an advertiser",
                                    options=advertisers_with_ads['handle'].tolist(),
                                    format_func=lambda h: advertisers_with_ads.set_index('handle').loc[h, 'label']
                                                          if h in advertisers_with_ads['handle'].values else h)
    if selected_handle:
        ads = f[f['handle'] == selected_handle].sort_values('first_shown')
        st.write(f"**{len(ads)} ads** for `@{selected_handle}`")
        for _, ad in ads.iterrows():
            with st.expander(f"ad_id {ad['ad_id']} — {ad['first_shown'].date() if pd.notna(ad['first_shown']) else '?'} → {ad['last_shown'].date() if pd.notna(ad['last_shown']) else '?'}  ·  {ad['kind']}  ·  reach {ad['reach_raw']}"):
                cA, cB = st.columns([3, 2])
                with cA:
                    # Try to play from local file first
                    local_dir = os.path.join(CREATIVES, ad['handle'])
                    found_file = None
                    if os.path.isdir(local_dir):
                        for fn in os.listdir(local_dir):
                            if fn.startswith(ad['ad_id']):
                                found_file = os.path.join(local_dir, fn)
                                break
                    if found_file and found_file.endswith('.mp4'):
                        st.video(found_file)
                    elif found_file and found_file.endswith('.jpg'):
                        st.image(found_file)
                    else:
                        st.info("Creative not downloaded locally — open via library link below")
                    st.markdown(f"[📺 Open in TikTok library]({ad['library_url']})")
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
                st.markdown(f"[Library]({ad['library_url']})  ·  [Profile](https://www.tiktok.com/@{ad['handle']})")
    else:
        st.info("Type a word or phrase to search ad transcripts. Useful queries: party names (ΑΚΕΛ, ΔΗΣΥ, ΕΛΑΜ), policy terms (εκποίηση, στέγη, ψηφίστε), candidate names.")

# ── Raw data ──────────────────────────────────────────────────────────────────
with tab_raw:
    st.subheader(f"All {len(f)} ads matching filters")
    show_cols = ['match_type', 'handle', 'matched_candidate', 'matched_party',
                 'matched_district', 'ad_id', 'first_shown', 'last_shown',
                 'ad_status', 'reach_raw', 'kind', 'profile_url', 'library_url']
    st.dataframe(f[show_cols], use_container_width=True, hide_index=True,
                 column_config={'profile_url': st.column_config.LinkColumn(),
                                'library_url': st.column_config.LinkColumn()})
    csv = f.to_csv(index=False).encode('utf-8')
    st.download_button("📥 Download filtered CSV", csv,
                       file_name=f"tiktok_ads_filtered_{date.today()}.csv",
                       mime="text/csv")
