"""Regression test: the canonical-in-deploy shared files in this repo
have not been edited locally in a way that diverges from the upstream
manifest.

This test only runs the deploy-side half of the check (it can't see the
main repo from CI). It verifies that the files listed below — which the
manifest in main-repo's sync_shared.py marks as `canonical='deploy'` —
are all present and import cleanly. The full bidirectional drift check
runs locally via `python sync_shared.py` in the main repo.

The list is hardcoded here to avoid coupling the deploy repo to the
main repo's filesystem path. If a new file is added to the manifest in
main-repo's sync_shared.py, mirror it here as well — that's the only
manual coupling between the two halves.

Run from the deploy repo:
  python -m pytest tests/test_shared_files_sync.py -v
Or standalone:
  python tests/test_shared_files_sync.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Mirror of sync_shared.MANIFEST entries with canonical='deploy'.
# Update both when a file moves in/out of deploy.
CANONICAL_IN_DEPLOY = [
    'app_tiktok.py',
    'refresh_ad_statuses.py',
    'status_change_report.py',
    'export_bulk_report.py',
    'discover_tiktok_ads.py',
    'discover_content_keywords.py',
    'tiktok_api.py',
    'db_lock.py',
    'find_canonical_post_urls.py',
]


def test_every_canonical_deploy_file_exists():
    """Every file the deploy repo claims canonical ownership of must
    actually live here."""
    missing = []
    for fname in CANONICAL_IN_DEPLOY:
        if not os.path.exists(os.path.join(ROOT, fname)):
            missing.append(fname)
    assert not missing, (
        f"Deploy repo is missing files it claims canonical ownership of: "
        f"{missing}. Check sync_shared.py MANIFEST in main repo."
    )


def test_tiktok_api_helper_importable():
    """tiktok_api.py must import cleanly — it's the central quirk-handler
    and every discovery script depends on it."""
    from tiktok_api import (
        resolve_disclosed_name, resolve_funded_by, is_numeric_handle_quirk,
    )
    # Smoke-call to make sure the function bodies aren't broken
    assert resolve_disclosed_name({'business_name': 'x'}) == 'x'
    assert resolve_funded_by({'business_name': '1', 'paid_for_by': '1'}) is None
    assert is_numeric_handle_quirk({'business_name': '123'}) is True


def test_db_lock_helper_importable():
    """db_lock.py must import cleanly — the discover + refresh scripts
    fail at runtime if it's broken."""
    from db_lock import db_lock, master_db_lock
    # master_db_lock is an alias for the same thing
    assert db_lock is master_db_lock


if __name__ == '__main__':
    import traceback
    tests = [
        ('every canonical-deploy file exists', test_every_canonical_deploy_file_exists),
        ('tiktok_api helper importable',       test_tiktok_api_helper_importable),
        ('db_lock helper importable',          test_db_lock_helper_importable),
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
