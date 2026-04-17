"""
Run Lock Module

Prevents overlapping scheduled executions using a simple file-based lock.

A lock file older than STALE_AFTER_MINUTES is treated as stale (left by a
crashed run) and will be overridden automatically. No external dependencies
or platform-specific APIs are required.
"""

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger('portfolio_automation.run_lock')

LOCK_FILE = Path("data/run.lock")
STALE_AFTER_MINUTES = 30


def acquire_run_lock(lock_file: Path = LOCK_FILE) -> bool:
    """
    Try to acquire the run lock.

    Returns True if the lock was successfully acquired (safe to proceed).
    Returns False if another run appears to be in progress.

    A lock file older than STALE_AFTER_MINUTES is treated as stale (e.g.
    from a previous crash) and will be replaced automatically.
    """
    if lock_file.exists():
        try:
            age = datetime.now() - datetime.fromtimestamp(lock_file.stat().st_mtime)
            if age < timedelta(minutes=STALE_AFTER_MINUTES):
                pid = lock_file.read_text().strip()
                logger.warning(
                    f"Run lock is active (PID {pid}, held for "
                    f"{age.seconds // 60}m {age.seconds % 60}s). "
                    f"Another run is in progress — exiting."
                )
                return False
            logger.info(f"Stale run lock found (age: {age}). Removing and proceeding.")
        except OSError:
            # File may have disappeared between exists() and stat() — safe to proceed
            pass

    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(str(os.getpid()))
    return True


def release_run_lock(lock_file: Path = LOCK_FILE) -> None:
    """Remove the run lock file, releasing the lock."""
    try:
        lock_file.unlink()
    except FileNotFoundError:
        pass
