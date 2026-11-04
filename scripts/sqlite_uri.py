"""SQLite URI construction.

A filesystem path interpolated into ``file:...?mode=ro`` is not safe: ``#`` starts
a URI fragment and ``?`` starts the query, so ``/backups/db#real-live`` resolves to
``/backups/db`` — a different database — and ``mode=ro`` can end up inside the
fragment and be ignored, letting SQLite create a new file where the caller believed
it was reading an existing one. Percent-encoding the path removes both failures.

Kept dependency-free so every script can import it without cycles.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote


def readonly_uri(path: Path | str) -> str:
    """Return a ``mode=ro`` URI for ``path`` with the path percent-encoded."""
    resolved = Path(path).resolve().as_posix()
    return f"file:{quote(resolved, safe='/:')}?mode=ro"
