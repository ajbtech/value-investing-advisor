# value-investing-advisor

A research tool that reads SEC filings and produces cited, quote-validated value-investing
dossiers. It runs locally, on your own machine, against free EDGAR data.

It screens a universe of US filers with deterministic SQL, reads the filings the way a
careful analyst would, and ends at a written, falsifiable thesis. A human places the
order, or doesn't.

**This is not investment advice.** It is a general-purpose research tool that makes no
individualized recommendations. Nothing it produces is a recommendation to buy or sell
anything.

**This repository is public; the tool is personal.** It is built for one person on their
own machine. You are welcome to read it or fork it, but it is not packaged for general
use, is not offered as a service, and comes with no support.

## Status

The pipeline runs end to end: ingest → extract → price → screen → Pass A, on real
filings rather than fixtures. Milestones 1, 2, 3 and 4 are done; valuation, the thesis
layer and the remaining analysis passes are not.

What works today:

- **A point-in-time store.** Facts are recorded by **when they were filed**, not when
  they were earned, and every version of a restated figure is kept. Prices carry the same
  rule, and both are read through one gateway that a structural test keeps honest.
- **A job table** that makes every unit of work idempotent and resumable, so an
  interrupted session loses at most one filing's worth of work.
- **Section extraction** from 10-Ks, with a confidence score per item so the analysis
  layer can refuse to reason over a bad parse.
- **Five deterministic screens** — Magic Formula, Piotroski, net-net, owner earnings
  yield, quality-at-price — as SQL over as-of tables, never blended into one score, with
  every exclusion counted by reason. `--compare 12,24` re-runs them a year and two years
  back and says whether a company has been cheap all along or just fell in.
- **Pass A**, the risk-factor diff (Item 1A) and the MD&A diff (Item 7), with every
  quoted claim string-matched against the source filing before it is stored. Across five
  companies and six passes so far: **44 findings, 0 dropped, 0% fabrication.**

Not built yet: the valuation engine, the thesis generator and bear pass, the decision
journal, the quarterly falsification re-check, and Passes B, C and D. `STATE.md` has the
current state in detail, including the limitations that are known and open.

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
uv run dossier extract --cik 320193    # pull Item sections out of that filer's 10-Ks
uv run dossier prices --all            # daily closes for every ingested filer
uv run dossier screen                  # the five screens, as of today
uv run dossier screen --as-of 2025-06-30   # ...or as they would have read on any date
uv run dossier screen --compare 12,24      # ...and what they said a year and two years ago
uv run dossier status                  # what is in the store, and what work is pending
uv run dossier resume                  # retry whatever failed or was interrupted
```

Every command takes `--json` and then emits parseable output on stdout and nothing else,
so the pipeline can be driven by a script or by an agent as easily as by hand.

Ingesting is safe to interrupt and safe to repeat: work already done is skipped. Stages
talk to each other only through the store, so `extract`, `prices` and `screen` all need
`ingest` to have run first.

Data lives outside this checkout, in your per-user data directory
(`%LOCALAPPDATA%\edgar-dossier` on Windows). Set `DOSSIER_DATA_DIR` to put it elsewhere.

## Driving it from Claude

The CLI is the machine interface; the intended way to use this day to day is to ask
Claude, which runs the commands and presents the results. `.claude/skills/dossier/`
teaches a Claude Code session how.

The analysis passes run through that same Claude Code session, on an existing Claude
subscription — **there is no API key in this project**, which is deliberate: a key that
does not exist cannot be published. Pass A runs in two halves with the model in the
middle:

```
uv run dossier analyze --pass a --cik 57131 --prepare --out input.json
# Claude reads both filings and writes findings.json
uv run dossier analyze --pass a --cik 57131 --load findings.json
uv run dossier analyze --pass a --cik 57131 --item 7 --prepare   # ...and the MD&A
```

Item 1A and Item 7 have a prompt each, because the two sections are different kinds of
evidence: Item 1A is the risks management chose to name, Item 7 is management explaining
its own numbers.

Every quote in `findings.json` is string-matched against the filing it claims to come
from, and anything that does not match is dropped rather than stored. The exit code is 1
when anything was dropped, and the fabrication rate is reported either way; that number
is the point of the whole design. `dossier.models` keeps an API path for work that has to
run unattended, and it needs no key until you use it.

One caveat worth stating plainly: the validator protects the *dossier*, not a chat
summary of it. The dossier output is the record; treat anything paraphrased in
conversation as a lens on it, and check the citation before acting on a number.

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
