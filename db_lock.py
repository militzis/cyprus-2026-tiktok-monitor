"""Cross-process file lock for master-DB write sessions.

SQLite has file-level locks (good for one process at a time), but two
Python processes both writing to politician_ads.db can still:
  - corrupt the side-cache JSON files
  - lose UPDATEs when both upsert the same ad_id concurrently
  - have one's BEGIN..COMMIT block silently overlap with the other's

This module provides a single `master_db_lock()` context manager that
every script wraps its write session in. Two processes holding the lock
serialize cleanly; the second waits up to LOCK_TIMEOUT seconds before
giving up with a clear error.

Usage:
    from db_lock import master_db_lock
    with master_db_lock():
        run_my_db_writes()

Lock file path is co-located with the master DB so it's on the same
filesystem (filelock relies on flock()/LockFileEx, which need
same-FS semantics).
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager

try:
    from filelock import FileLock, Timeout
except ImportError:
    sys.exit(
        "ERROR: db_lock requires the 'filelock' package — install with:\n"
        "    pip install filelock"
    )

MASTER_DB    = r'C:\Users\milit\meta_pipeline_data\politician_ads.db'
LOCK_TIMEOUT = 600   # wait up to 10 min before giving up


@contextmanager
def db_lock(db_path: str = MASTER_DB, timeout: float = LOCK_TIMEOUT):
    """Acquire an exclusive lock on a DB write session.

    Args:
      db_path: the SQLite DB file we're about to write to. The lock file
        is `<db_path>.lock` (co-located so it's on the same filesystem).
        Default is the master DB.
      timeout: max seconds to wait for the lock. Default 600 (10 min) —
        long enough for a typical refresh_ad_statuses run to finish.

    Raises:
      SystemExit (via filelock.Timeout) if we couldn't acquire after
      `timeout` seconds. The error message includes the path so a human
      can identify and kill the stuck process.

    Lock file is automatically released on context exit (success or
    exception). The lock file itself persists on disk but is empty.
    """
    lock_path = db_path + '.lock'
    parent = os.path.dirname(lock_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    lock = FileLock(lock_path, timeout=timeout)
    try:
        with lock:
            yield
    except Timeout:
        sys.exit(
            f"ERROR: could not acquire DB lock at {lock_path} within "
            f"{timeout}s. Another writer is holding it. Identify the "
            f"holder, let it finish, or rm the .lock file ONLY if "
            f"you've verified no other process is writing."
        )


# Convenience alias for the common case
master_db_lock = db_lock
