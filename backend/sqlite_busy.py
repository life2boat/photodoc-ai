"""Bounded retries for database-only operations which roll back before replay."""
import functools
import random
import sqlite3
import time

BUSY_TIMEOUT = 0.1
RETRY_BUDGET = 4.0


def is_busy(error):
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        # Numeric primary codes are stable, also on Python 3.10 without the
        # exported SQLITE_* constants. Mask extended result codes.
        return (code & 255) in (5, 6)
    return str(error).lower() in ("database is locked", "database table is locked")


def retry_busy(operation):
    """Never wrap SMTP, upload, unlink, or any transaction with external effects."""
    @functools.wraps(operation)
    def bounded(*args, **kwargs):
        deadline = time.monotonic() + RETRY_BUDGET
        delay = 0.01
        while True:
            try:
                return operation(*args, **kwargs)
            except sqlite3.OperationalError as error:
                remaining = deadline - time.monotonic()
                if not is_busy(error) or remaining <= 0:
                    raise
                time.sleep(min(remaining, delay + random.uniform(0, delay / 4)))
                delay = min(0.2, delay * 2)
    return bounded
