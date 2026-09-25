"""EDGAR's nightly `companyfacts.zip`: every filer's XBRL facts in one download.

Ingest keeps about forty of the thousands of tags a filer reports, so widening the tag
set used to mean fetching every filer's `companyfacts` again. The ZIP is one request
instead of twelve thousand, and kept on disk it makes a tag change a local re-parse.

It holds one `CIK##########.json` per filer, each the same document the per-company API
returns. A filer with no XBRL has no entry, which is the ZIP's way of saying what the
API says with a 404.
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path

COMPANYFACTS_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"


def companyfacts_version(path: Path | str) -> str:
    """When the SEC built this ZIP, from the newest entry's timestamp.

    Not the local file's modification time, which a copy or a restore changes. The same
    ZIP downloaded twice is one version; last night's and tonight's are two.
    """
    with zipfile.ZipFile(path) as zf:
        newest = max((info.date_time for info in zf.infolist()), default=(1980, 1, 1, 0, 0, 0))
    return datetime(*newest).isoformat()


def read_company_facts(zf: zipfile.ZipFile, cik: int) -> dict | None:
    """One filer's `companyfacts` document, or None if the ZIP has no entry for it."""
    try:
        with zf.open(f"CIK{int(cik):010d}.json") as handle:
            return json.load(handle)
    except KeyError:
        return None
