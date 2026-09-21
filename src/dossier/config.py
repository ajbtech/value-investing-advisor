"""Where things live.

Code in the repo, data on the user's machine. The store runs to tens of gigabytes and
is rebuildable from EDGAR, so it never belongs in a checkout. The decision journal is
personal and belongs somewhere the user chooses — their own private repo, if they want
timestamp integrity.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "edgar-dossier"

#: Overrides, mostly for tests and for users who keep large data on another disk.
DATA_DIR_ENV = "DOSSIER_DATA_DIR"
JOURNAL_DIR_ENV = "DOSSIER_JOURNAL_DIR"
USER_AGENT_ENV = "EDGAR_USER_AGENT"


@dataclass(frozen=True)
class Config:
    data_dir: Path
    journal_dir: Path

    @classmethod
    def load(cls) -> Config:
        data_dir = Path(os.environ.get(DATA_DIR_ENV) or user_data_dir(APP_NAME, appauthor=False))
        journal = os.environ.get(JOURNAL_DIR_ENV)
        return cls(
            data_dir=data_dir,
            journal_dir=Path(journal) if journal else data_dir / "journal",
        )

    @property
    def store_path(self) -> Path:
        return self.data_dir / "edgar.sqlite"

    @property
    def output_dir(self) -> Path:
        """Job output. The LLM analysis cache lands here, and is worth backing up —
        it is the only artifact in the system that costs money to recreate."""
        return self.data_dir / "jobs"
