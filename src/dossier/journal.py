"""The decision journal: one file per entry, never edited.

Theses, bear passes, pass-overs and re-checks all write here. The journal lives in a
directory the user chooses — never in this public repository — and it is the one
artifact in the system that cannot be rebuilt from EDGAR.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path


def append_entry(journal_dir: Path | None, entry: dict, stem: str) -> str | None:
    """Write one decision as its own file, never touching an existing one.

    Append-only in the plainest sense: a new name every time, so nothing that was
    written can be revised by writing again. Returns the path, or None when no journal
    directory is configured.
    """
    if journal_dir is None:
        return None
    directory = Path(journal_dir)
    directory.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    path = directory / f"{stamp}-{stem}.json"
    suffix = 1
    while path.exists():
        suffix += 1
        path = directory / f"{stamp}-{stem}-{suffix}.json"

    # Written and fsynced before anything reports success, for the same reason the job
    # table does it: the alternative loses work on a crash, and loses it silently.
    with path.open("w", encoding="utf-8") as handle:
        json.dump(entry, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    return str(path)
