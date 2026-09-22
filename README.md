# value-investing-advisor

A research tool that reads SEC filings with LLM agents and produces cited
value-investing dossiers. It runs locally, on your own machine, with your own API keys.

It screens the full universe of US filers, reads the filings the way a careful analyst
would, and ends at a written, falsifiable thesis. A human places the order, or doesn't.

**This is not investment advice.** It is a general-purpose research tool that makes no
individualized recommendations. Nothing it produces is a recommendation to buy or sell
anything.

**This repository is public; the tool is personal.** It is built for one person on their
own machine. You are welcome to read it or fork it, but it is not packaged for general
use, is not offered as a service, and comes with no support.

## Status

Early. Milestone 1 of 10 — the point-in-time data layer — is in place:

- A SQLite store that records facts by **when they were filed**, not when they were
  earned, and keeps every version of a restated figure.
- A job table that makes every unit of work idempotent and resumable, so an interrupted
  session loses at most one filing's worth of work.
- A rate-limited EDGAR client that refuses to start without a valid declared
  `User-Agent`, rather than letting the SEC block you.
- Ingest from EDGAR's free JSON endpoints into that store.

Not built yet: section extraction, the screens, the analysis passes, valuation, the
thesis and journal, and the local web app.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```
uv sync --extra dev
```

## Use

The SEC requires a declared `User-Agent` naming a human and an email address. Without
one your requests are refused as an "Undeclared Automated Tool".

```
export EDGAR_USER_AGENT="Jane Doe jane@example.com"   # PowerShell: $env:EDGAR_USER_AGENT = "..."

uv run dossier ingest --cik 320193     # one filer
uv run dossier ingest --limit 500      # the first 500 filers in the ticker map
uv run dossier status                  # what is in the store, and what work is pending
uv run dossier resume                  # retry whatever failed or was interrupted
```

Every command takes `--json` and then emits parseable output on stdout and nothing else,
so the pipeline can be driven by a script or by an agent as easily as by hand.

Ingesting is safe to interrupt and safe to repeat: work already done is skipped.

Data lives outside this checkout, in your per-user data directory
(`%LOCALAPPDATA%\edgar-dossier` on Windows). Set `DOSSIER_DATA_DIR` to put it elsewhere.

## Driving it from Claude

The CLI is the machine interface; the intended way to use this day to day is to ask
Claude, which runs the commands and presents the results. `.claude/skills/dossier/`
teaches a Claude Code session how.

The analysis passes run through that same Claude Code session, on an existing Claude
subscription — **there is no API key in this project**, which is deliberate: a key that
does not exist cannot be published. `dossier.models` keeps an API path for work that has
to run unattended, and it needs no key until you use it.

One caveat worth stating plainly: the pipeline validates every quoted claim against its
source filing before it enters a dossier, but that validation protects the *dossier*,
not a chat summary of it. The dossier output is the record; treat anything paraphrased
in conversation as a lens on it, and check the citation before acting on a number.

## Develop

Test-driven: the failing test comes first. See `CLAUDE.md` for the invariants that hold
across the whole codebase, and `STATE.md` for where the build currently stands.

```
uv run pytest                # the suite; nothing here touches the network
uv run pytest -m network     # the few live-SEC tests, deselected by default
uv run ruff check . && uv run ruff format --check .
```

## Licence

MIT. See `LICENSE`.
