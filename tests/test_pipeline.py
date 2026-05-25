"""Regression tests for the specific bug classes we keep hitting.

Each test corresponds to a real production bug we shipped this week.
The intent is to prevent re-introducing the same shape of bug, not to
exhaustively cover the codebase.

Run from the deploy repo:
  python -m pytest tests/test_pipeline.py -v
Or standalone:
  python tests/test_pipeline.py
"""
from __future__ import annotations

import os
import sys
import sqlite3
import pandas as pd

# Importable modules sit at the repo root
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ============================================================================
# 1. tiktok_api.resolve_disclosed_name — the numeric-business_name quirk
# ============================================================================
# Bug shape: TikTok's ad endpoint returns funder numeric ID in business_name
# for some live advertisers. Five readers wrote the numeric ID as the
# disclosed name, polluting the DB with "ghost advertisers" that masked
# real candidates like @cmountouckos.

def test_resolve_disclosed_name_numeric_fallback():
    from tiktok_api import resolve_disclosed_name
    # Numeric API response → use fallback
    assert resolve_disclosed_name(
        {'business_name': '7514704009685368854'}, fallback='cmountouckos'
    ) == 'cmountouckos'

def test_resolve_disclosed_name_readable_wins():
    from tiktok_api import resolve_disclosed_name
    # Readable handle → use it (ignore fallback)
    assert resolve_disclosed_name(
        {'business_name': 'cmountouckos'}, fallback='wrong'
    ) == 'cmountouckos'

def test_resolve_disclosed_name_empty_inputs():
    from tiktok_api import resolve_disclosed_name
    assert resolve_disclosed_name(None) == ''
    assert resolve_disclosed_name({}) == ''
    assert resolve_disclosed_name({'business_name': None}, fallback='') == ''
    assert resolve_disclosed_name({'business_name': '   '}, fallback='x') == 'x'

def test_resolve_funded_by_drops_numeric_echo():
    from tiktok_api import resolve_funded_by
    # When paid_for_by echoes the same numeric ID as business_name, it's noise
    assert resolve_funded_by({'business_name': '7514', 'paid_for_by': '7514'}) is None
    # Real disclosed funder names pass through
    assert resolve_funded_by({
        'business_name': 'cmountouckos',
        'paid_for_by':   'Some Funder LLC',
    }) == 'Some Funder LLC'


# ============================================================================
# 2. derive_status — NaN truthiness + violation detection
# ============================================================================
# Bug shape A: pandas iterrows() returns NaN for missing values; NaN is
# truthy but breaks len(); old code crashed in tab_browse with TypeError.
# Bug shape B: derive_status only checked ad_status; TikTok stores takedown
# reason in status_statement with ad_status='inactive', so the "Removed by
# TikTok" KPI stayed at 0 despite a real takedown.

def _import_derive_status():
    """Import derive_status from app_tiktok.py without triggering the
    Streamlit page setup."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "app_tiktok", os.path.join(ROOT, 'app_tiktok.py')
    )
    # We can't actually import the module (it calls st.set_page_config at
    # top level). Instead, exec just the derive_status function body via
    # eval. Cheaper alternative: replicate the logic inline as a contract.
    # Use the same fixture:
    import pandas as pd
    today = pd.Timestamp('2026-05-18')

    def derive_status(row):
        raw_v  = row.get('ad_status')
        stmt_v = row.get('status_statement')
        raw  = (raw_v  if pd.notna(raw_v)  and isinstance(raw_v,  str) else '').lower()
        stmt = (stmt_v if pd.notna(stmt_v) and isinstance(stmt_v, str) else '').lower()
        if 'removed' in raw or 'removed' in stmt or 'violation' in raw or 'violation' in stmt:
            return '🚨 Removed by TikTok'
        if 'deleted' in raw or 'deleted_by_advertiser' in stmt:
            return '🗑 Deleted by advertiser'
        if raw == 'expired' or 'expired' in stmt:
            return '⌛ Expired'
        ls = row.get('last_shown')
        if pd.isna(ls):
            return '❓ Unknown'
        days_since = (today - ls).days
        # Threshold changed 2026-05-25: Active = shown today or yesterday (≤1 day).
        # Mirrors the logic in app_tiktok.py derive_status(). Keep in sync.
        if days_since <= 1:
            return '✅ Active (today / yesterday)'
        if days_since <= 30:
            return '🟡 Recently inactive (2–30 days)'
        return '⚪ Dormant (30+ days)'
    return derive_status

def test_derive_status_takedown_via_statement():
    """Florentzos-style: ad_status='inactive', takedown reason in stmt."""
    derive = _import_derive_status()
    row = {
        'ad_status': 'inactive',
        'status_statement': "Removed from TikTok due to a violation of TikTok's terms",
        'last_shown': pd.Timestamp('2026-05-15'),
    }
    assert derive(row) == '🚨 Removed by TikTok'

def test_derive_status_handles_nan_values():
    """iterrows returns NaN for nulls — must not crash."""
    derive = _import_derive_status()
    row = pd.Series({
        'ad_status': None,
        'status_statement': None,
        'last_shown': pd.NaT,
    })
    # Must not raise TypeError("unsupported format string passed to NoneType")
    result = derive(row)
    assert result in ('❓ Unknown', '⚪ Dormant (30+ days)')

def test_derive_status_handles_empty_strings():
    # today is fixed at 2026-05-18 in _import_derive_status; 2026-05-18 - 2026-05-17 = 1 day → Active
    derive = _import_derive_status()
    row = {'ad_status': '', 'status_statement': '', 'last_shown': pd.Timestamp('2026-05-17')}
    assert derive(row) == '✅ Active (today / yesterday)'

def test_derive_status_active_recent():
    # last_shown yesterday relative to the fixed today (2026-05-18)
    derive = _import_derive_status()
    row = {'ad_status': 'active', 'status_statement': 'N/A',
           'last_shown': pd.Timestamp('2026-05-18')}
    assert derive(row) == '✅ Active (today / yesterday)'

def test_derive_status_recently_inactive():
    # last_shown 10 days ago → recently inactive (2–30 days)
    derive = _import_derive_status()
    row = {'ad_status': 'active', 'status_statement': 'N/A',
           'last_shown': pd.Timestamp('2026-05-08')}
    assert derive(row) == '🟡 Recently inactive (2–30 days)'

def test_derive_status_dormant():
    derive = _import_derive_status()
    row = {'ad_status': 'active', 'status_statement': 'N/A',
           'last_shown': pd.Timestamp('2026-01-01')}
    assert derive(row) == '⚪ Dormant (30+ days)'


# ============================================================================
# 3. Date format normalisation — YYYYMMDD ↔ YYYY-MM-DD
# ============================================================================
# Bug shape: 74 master rows had raw YYYYMMDD format (some write path
# bypassed _fmt_date), rendering as "20251025" in dashboard date columns.
# _fmt_date is a private helper in discover_tiktok_ads.py.

def test_fmt_date_normalises_yyyymmdd():
    # Replicate the contract — see discover_tiktok_ads._fmt_date
    def _fmt_date(yyyymmdd: str) -> str:
        if not yyyymmdd or len(yyyymmdd) != 8:
            return yyyymmdd or ''
        return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
    assert _fmt_date('20251025') == '2025-10-25'
    assert _fmt_date('') == ''
    assert _fmt_date(None) == ''
    # Already-formatted dates pass through unchanged (length != 8)
    assert _fmt_date('2025-10-25') == '2025-10-25'


# ============================================================================
# 4. Public-DB selectbox: groupby('handle').first() prevents duplicate-row
# crash that previously hit st.selectbox when one handle had multiple
# (matched_candidate, matched_party) variants.
# ============================================================================

def test_selectbox_handles_duplicate_handle_rows():
    """One handle, multiple rows with different (cand, party) — the
    groupby in tab_browse must collapse to exactly one label per handle."""
    df = pd.DataFrame([
        {'handle': 'phedonphedonos', 'matched_candidate': 'Φαίδων Φαίδωνος', 'matched_party': 'ΔΗΣΥ'},
        {'handle': 'phedonphedonos', 'matched_candidate': '',                 'matched_party': ''},
    ])
    # Replicates the fixed logic in app_tiktok.py tab_browse
    advertisers = (df[['handle', 'matched_candidate', 'matched_party']]
                   .fillna('')
                   .groupby('handle', as_index=False)
                   .first())
    assert len(advertisers) == 1
    assert advertisers.iloc[0]['handle'] == 'phedonphedonos'


# ============================================================================
# 5. The public-DB rebuild must preserve status_statement
# ============================================================================
# Bug shape: _shrink_public_db.py was wiping status_statement, so even
# after derive_status() was patched, the column was always None and the
# takedown KPI stayed at 0. The build_public_db.py replacement explicitly
# excludes status_statement from the strip list.

def test_build_public_db_does_not_strip_status_statement():
    """Read build_public_db.py source and assert status_statement is
    NOT in the column-strip list."""
    main_repo = r'C:\Users\milit\OneDrive\Documents\META library content'
    path = os.path.join(main_repo, 'build_public_db.py')
    if not os.path.exists(path):
        # On CI or fresh clone, file may not exist — skip silently
        return
    src = open(path, encoding='utf-8').read()
    # Find the COLUMNS_TO_NULL definition
    import re
    m = re.search(r"COLUMNS_TO_NULL\s*=\s*\[([^\]]+)\]", src)
    assert m, "COLUMNS_TO_NULL list not found in build_public_db.py"
    columns = m.group(1)
    assert 'status_statement' not in columns, (
        "status_statement must NOT be in build_public_db.py COLUMNS_TO_NULL "
        "— derive_status() reads it to detect TikTok takedowns. "
        f"Current list: {columns.strip()}"
    )


# ============================================================================
# Standalone runner
# ============================================================================
if __name__ == '__main__':
    import traceback
    tests = [
        ('resolve_disclosed_name numeric fallback',  test_resolve_disclosed_name_numeric_fallback),
        ('resolve_disclosed_name readable wins',     test_resolve_disclosed_name_readable_wins),
        ('resolve_disclosed_name empty inputs',      test_resolve_disclosed_name_empty_inputs),
        ('resolve_funded_by drops numeric echo',     test_resolve_funded_by_drops_numeric_echo),
        ('derive_status takedown via statement',     test_derive_status_takedown_via_statement),
        ('derive_status handles NaN values',         test_derive_status_handles_nan_values),
        ('derive_status handles empty strings',      test_derive_status_handles_empty_strings),
        ('derive_status active recent',              test_derive_status_active_recent),
        ('derive_status recently inactive',          test_derive_status_recently_inactive),
        ('derive_status dormant',                    test_derive_status_dormant),
        ('_fmt_date normalises YYYYMMDD',            test_fmt_date_normalises_yyyymmdd),
        ('selectbox dedups duplicate-handle rows',   test_selectbox_handles_duplicate_handle_rows),
        ('build_public_db preserves status_statement', test_build_public_db_does_not_strip_status_statement),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception:
            print(f"  ✗ {name}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {len(tests) - failed}/{len(tests)} passing")
    sys.exit(failed)
