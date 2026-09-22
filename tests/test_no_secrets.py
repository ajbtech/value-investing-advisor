"""Nothing secret, and nothing personal, in a public repository.

This repository is public. The tool is personal, which makes it tempting to hardcode
a key or an email address "just for me" — and that is exactly how they get published.
A rule nobody checks lasts until the first hurried afternoon, so this checks.

It scans git-tracked files only. Anything gitignored is, by definition, not published.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: Reserved for documentation by RFC 2606 and RFC 6761 — nobody's real inbox. Only
#: these are allowed, so the guard never needs an exception for an invented domain:
#: allowlisting those one at a time is how a check like this quietly stops working.
PLACEHOLDER_DOMAINS = (
    ".example.com",
    ".example.net",
    ".example.org",
    ".example",
    ".invalid",
    ".test",
)
PLACEHOLDER_ADDRESSES = ("your@email.com", "noreply@anthropic.com")

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

SECRETS = {
    "anthropic key": re.compile(r"sk-ant-[A-Za-z0-9\-_]{8,}"),
    "openai key": re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    "aws access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # No leading \b: a prefixed name like ANTHROPIC_API_KEY has a word character
    # before "API", so a boundary there never matches and the check silently passes.
    "assigned credential": re.compile(
        r"""(?i)[\w.\-]*(api[_-]?key|secret|password|auth[_-]?token)"""
        r"""\s*[:=]\s*["'][^"'\s]{12,}["']"""
    ),
}


def tracked_text_files() -> list[Path]:
    if shutil.which("git") is None:  # pragma: no cover - git is present in CI
        pytest.skip("git unavailable, cannot enumerate tracked files")
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    paths = []
    for name in listing.stdout.split("\0"):
        if not name:
            continue
        path = REPO / name
        # This file necessarily contains the patterns it searches for.
        if path.resolve() == Path(__file__).resolve() or not path.is_file():
            continue
        paths.append(path)
    return paths


def readable(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None  # a binary fixture; nothing to scan


@pytest.fixture(scope="module")
def tracked() -> list[tuple[Path, str]]:
    files = []
    for path in tracked_text_files():
        text = readable(path)
        if text is not None:
            files.append((path, text))
    return files


def test_there_are_tracked_files_to_scan(tracked):
    """Guards the guard: a scan of nothing passes trivially and proves nothing."""
    assert len(tracked) > 10


@pytest.mark.parametrize("label", sorted(SECRETS))
def test_no_credentials_are_committed(tracked, label):
    pattern = SECRETS[label]
    offenders = [
        f"{path.relative_to(REPO)}: {pattern.search(text).group(0)[:12]}…"
        for path, text in tracked
        if pattern.search(text)
    ]
    assert offenders == [], f"{label} found in tracked files: {offenders}"


def test_no_real_email_addresses_are_committed(tracked):
    """The EDGAR User-Agent has to carry a real name and email, and the SEC sees it on
    every request. It belongs in an environment variable, never in a tracked file."""
    offenders = []
    for path, text in tracked:
        for match in EMAIL.finditer(text):
            address = match.group(0)
            if address in PLACEHOLDER_ADDRESSES:
                continue
            domain = address.rsplit("@", 1)[1].lower()
            if domain in ("example.com", "example.net", "example.org"):
                continue
            if domain.endswith(PLACEHOLDER_DOMAINS):
                continue
            offenders.append(f"{path.relative_to(REPO)}: {address}")
    assert offenders == [], f"real-looking addresses in tracked files: {offenders}"


def test_the_env_file_is_not_tracked(tracked):
    assert not any(path.name == ".env" for path, _ in tracked)


def test_the_store_is_not_tracked(tracked):
    """Tens of gigabytes, rebuildable from EDGAR, and none of GitHub's business."""
    assert not any(path.suffix in {".sqlite", ".sqlite3", ".db"} for path, _ in tracked)


class TestTheGuardItself:
    """A secrets check that cannot fire is worse than none: it reports all-clear.

    These are synthetic strings, not files, so the guard is verified on every run
    rather than only when somebody remembers to plant a leak.
    """

    CAUGHT = [
        ("sk-ant-api03-" + "A" * 20, "anthropic key"),
        ("sk-" + "B" * 32, "openai key"),
        ("AKIAIOSFODNN7EXAMPLE", "aws access key"),
        ("ghp_" + "c" * 36, "github token"),
        ("-----BEGIN RSA PRIVATE KEY-----", "private key block"),
        ('ANTHROPIC_API_KEY = "not-an-sk-prefixed-value"', "assigned credential"),
        ('api_key: "abcdefghijklmnop"', "assigned credential"),
        ('password = "hunter2hunter2hunter2"', "assigned credential"),
    ]

    @pytest.mark.parametrize("sample,label", CAUGHT)
    def test_a_planted_credential_is_caught(self, sample, label):
        assert SECRETS[label].search(sample), f"{label} failed to match {sample[:20]}…"

    @pytest.mark.parametrize(
        "sample",
        [
            'EDGAR_USER_AGENT="Jane Doe jane@example.com"',
            "api_key is read from the environment",
            'store_path = "edgar.sqlite"',
            'help="a filer to ingest; repeatable"',
        ],
    )
    def test_ordinary_code_is_not_flagged(self, sample):
        """A guard that cries wolf gets switched off, and then guards nothing."""
        assert not any(pattern.search(sample) for pattern in SECRETS.values())

    @pytest.mark.parametrize(
        "address", ["alex.burtness@gmail.com", "someone@acme.co.uk", "me@mycompany.io"]
    )
    def test_a_real_looking_address_is_not_treated_as_a_placeholder(self, address):
        domain = address.rsplit("@", 1)[1].lower()
        assert address not in PLACEHOLDER_ADDRESSES
        assert domain not in ("example.com", "example.net", "example.org")
        assert not domain.endswith(PLACEHOLDER_DOMAINS)
