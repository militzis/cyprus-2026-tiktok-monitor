"""Regression test for the class of bug that hid the @florentzos_karayiannas
takedown today: load_ads()'s SELECT didn't include `status_statement`, so
derive_status() always saw None and never marked the row as "Removed by
TikTok" — even though the data was correctly stored in the public DB.

This test:
  1. Parses load_ads()'s SELECT statement to determine which DB columns
     the dashboard actually receives.
  2. Greps the rest of app_tiktok.py for every column reference of the
     form  ad['<col>']  /  row['<col>']  /  f['<col>']  /  df['<col>']  /
     row.get('<col>')  /  ad.get('<col>')
  3. Asserts every referenced column is either:
       (a) in the SELECT, or
       (b) a derived column added by code after load (e.g. derived_status,
           days_active, profile_url, library_url, kind, label, transition)

Run from the deploy repo:
  python -m pytest tests/test_dashboard_select_completeness.py -v
Or standalone:
  python tests/test_dashboard_select_completeness.py
"""
from __future__ import annotations

import os
import re
import sys
import sqlite3


ROOT      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_FILE  = os.path.join(ROOT, 'app_tiktok.py')
PUBLIC_DB = os.path.join(ROOT, 'politician_ads_public.db')

# Columns the code adds AFTER loading from the DB (not expected in SELECT).
# Keep this list in sync with `app_tiktok.py`'s post-load .assign / .apply
# calls and the column-config aliases below.
DERIVED_COLUMNS = {
    # Per-row derived columns added by app_tiktok.py after load_ads()
    'derived_status',    # derive_status(row) → KPI bucket
    'days_active',       # (last_shown - first_shown).dt.days + 1
    'profile_url',       # f"https://www.tiktok.com/@{handle}" (handle column)
    'library_url',       # f"https://library.tiktok.com/ads/detail/?ad_id={ad_id}"
    'kind',              # 'VIDEO' / 'IMAGE' / '?' from videos_json/image_urls_json
    'label',             # selectbox display label (in tab_browse)
    # JOIN columns surfaced via the tiktok_ad_status_changes query
    'transition',        # f"{prev} → {new}" string in status-change history
    'cdn_url',           # first video URL or first image URL in changes table
    'matched_district',  # sometimes referenced via .get when row came from changes (JOIN)
    'new_statement',     # from tiktok_ad_status_changes JOIN
    'prev_status',       # from tiktok_ad_status_changes JOIN
    'new_status',        # from tiktok_ad_status_changes JOIN
    'observed_at',       # from tiktok_ad_status_changes JOIN
    # Synthetic columns from groupby().agg() / value_counts() reshapes
    'ads',               # .agg(ads=('ad_id','count'))
    'first',             # .agg(first=('first_shown','min'))
    'last',              # .agg(last=('last_shown','max'))
    'active',            # .agg(active=...sum of ad_status=='active')
    'profile',           # top['profile'] in tab_party
    'latest_ad',         # top['latest_ad'] in tab_party
    '_latest_ad_id',     # internal join key for latest_ad
    'roster_size',       # candidates_df.groupby('party').size() rename
    # Review-queue tab columns — renamed in groupby().agg()
    'auto_verdict',      # aliased from auto_review_verdict
    'auto_confidence',   # aliased from auto_review_confidence
    'auto_reason',       # aliased from auto_review_reason
    'auto_disagrees',    # computed post-groupby
    'max_reach_upper',   # .agg(max_reach_upper=('times_shown_upper_bound','max'))
    'first_seen',        # .agg(first_seen=('first_shown','min'))
    'last_seen',         # .agg(last_seen=('last_shown','max'))
    'has_transcript',    # .agg(has_transcript=('transcript', notna.any))
}

# Column aliases — load_ads() does `advertiser_disclosed_name AS handle`,
# so 'handle' in code maps to a real DB column.
SELECT_ALIASES = {
    'handle': 'advertiser_disclosed_name',
}


def extract_select_columns(src: str) -> set[str]:
    """Find load_ads() and return the bare column names from its SELECT.
    Handles both plain `pd.read_sql_query(\"\"\"...\"\"\")` and f-string
    `pd.read_sql_query(f\"\"\"...{cols}\"\"\")` forms — but resolves any
    f-string interpolation by evaluating the corresponding variable
    assignment that precedes the call."""
    m = re.search(r'def\s+load_ads\b.*?pd\.read_sql_query\(\s*f?"""(.+?)"""',
                  src, re.DOTALL)
    if not m:
        raise AssertionError("load_ads() / pd.read_sql_query not found in app_tiktok.py")
    sql = m.group(1)
    # If the SQL contains `{auto_cols}` (or similar) interpolation,
    # try to resolve it by finding `auto_cols = ...` assignment above
    # and substituting its string literal value into the SQL.
    for placeholder in re.findall(r'\{(\w+)\}', sql):
        # Look for the variable's string-literal definition
        assigns = re.findall(
            rf'\b{placeholder}\s*=\s*(?:"([^"]+)"|\'([^\']+)\')',
            src,
        )
        # Also look for ternary forms `var = X if ... else Y` — concat both
        ternary = re.findall(
            rf'\b{placeholder}\s*=\s*\(?\s*"([^"]+)"\s*if[^)]*else\s*"([^"]+)"',
            src,
        )
        substitutions = []
        for a in assigns:
            for v in a:
                if v:
                    substitutions.append(v)
        for tup in ternary:
            substitutions.extend(t for t in tup if t)
        sql = sql.replace('{' + placeholder + '}',
                          ', '.join(substitutions) if substitutions else '')
    # Find the SELECT … FROM segment
    select_match = re.search(r'SELECT\s+(.+?)\s+FROM\s+', sql, re.IGNORECASE | re.DOTALL)
    assert select_match, "no SELECT … FROM in load_ads()"
    cols_raw = select_match.group(1)
    # Strip comments, whitespace, split on commas
    cols = []
    for col in cols_raw.split(','):
        col = col.strip()
        # Handle  `colname AS alias`  → take the alias (that's what the DataFrame sees)
        am = re.search(r'\bAS\s+(\w+)\s*$', col, re.IGNORECASE)
        if am:
            cols.append(am.group(1))
        else:
            # Plain column (may have a table prefix)
            cols.append(col.split('.')[-1])
    return {c for c in cols if c}


def extract_referenced_columns(src: str) -> set[str]:
    """Find every  X['col']  /  X.get('col')  in the file (X in
    ad/row/f/df/changes/r), excluding column references inside the SELECT
    string itself."""
    # Drop the load_ads() docstring + SQL to avoid double-counting
    # (SELECT col names look like references but aren't)
    src_no_sql = re.sub(r'pd\.read_sql_query\(\s*""".*?"""', '<<SQL>>', src,
                        flags=re.DOTALL)
    # Strip CSS/HTML/markdown columns inside st.column_config (those are
    # column-config keys, all of which match real or derived column names)
    references = set()
    for pattern in (
        r"\b(?:ad|row|f|df|r|changes|removed|status_filtered|hits)\s*\[\s*['\"]([a-z_]+)['\"]\s*\]",
        r"\b(?:ad|row|f|df|r|changes|removed|status_filtered|hits)\.get\(\s*['\"]([a-z_]+)['\"]",
    ):
        for m in re.finditer(pattern, src_no_sql):
            references.add(m.group(1))
    return references


def assert_db_has_columns(cols: set[str]) -> None:
    """Confirm the live public DB actually has all the columns the dashboard
    expects. Catches the case where someone ALTERed away a column."""
    if not os.path.exists(PUBLIC_DB):
        # Local dev without a public DB — skip silently
        return
    conn = sqlite3.connect(PUBLIC_DB)
    db_cols = {r[1] for r in conn.execute("PRAGMA table_info(tiktok_ads)")}
    conn.close()
    # Map aliases back to real column names
    expected = {SELECT_ALIASES.get(c, c) for c in cols}
    missing  = expected - db_cols - DERIVED_COLUMNS - {'handle'}
    assert not missing, (
        f"Public DB tiktok_ads is missing columns the SELECT requests: {sorted(missing)}"
    )


def test_dashboard_select_includes_every_referenced_column():
    src = open(APP_FILE, encoding='utf-8').read()
    selected = extract_select_columns(src)
    referenced = extract_referenced_columns(src)
    missing = referenced - selected - DERIVED_COLUMNS

    if missing:
        msg = (
            f"\nDashboard reads columns that load_ads() doesn't SELECT:\n"
            f"  missing: {sorted(missing)}\n"
            f"  selected: {sorted(selected)}\n"
            f"  derived (allow-listed): {sorted(DERIVED_COLUMNS)}\n\n"
            f"FIX: add the missing columns to load_ads()'s SELECT in {APP_FILE},\n"
            f"OR if they are computed after load, add them to DERIVED_COLUMNS\n"
            f"in this test file."
        )
        raise AssertionError(msg)


def test_public_db_has_every_selected_column():
    src = open(APP_FILE, encoding='utf-8').read()
    selected = extract_select_columns(src)
    assert_db_has_columns(selected)


if __name__ == '__main__':
    tests = [
        ('SELECT covers every code reference', test_dashboard_select_includes_every_referenced_column),
        ('public DB has every selected column', test_public_db_has_every_selected_column),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as e:
            print(f"  ✗ {name}")
            print(f"    {e}")
            failed += 1
    sys.exit(failed)
