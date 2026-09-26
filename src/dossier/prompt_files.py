"""The pinned prompts every model-facing stage hands over.

Prompts are files in `dossier/prompts/`, one per version, so a result can always be
traced to the exact words that produced it. Analysis, valuation and thesis all read
them; none of them should have to import another stage to do so.
"""

from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"


def prompt_text(version: str) -> str:
    path = PROMPTS_DIR / f"{version}.md"
    if not path.exists():
        raise FileNotFoundError(
            f"no prompt {version!r} in {PROMPTS_DIR}. Prompt versions are pinned in the "
            "repository; add the file rather than inlining the text."
        )
    return path.read_text(encoding="utf-8")
