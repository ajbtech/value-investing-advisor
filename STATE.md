# STATE.md

Where the build stands. A cold session should be able to read this and start work without
re-deriving the plan. Read `CLAUDE.md` first for the invariants; this file is only status.

The build plan lives outside the repo, as a Claude doc:
<https://claude.ai/artifact/MtKsvd1pve5CEkgcxsXQ47>

## Current milestone

**Milestone 1 — job table + EDGAR ingest into SQLite, ~500 filers, `filed_date` enforced.**

## Done

**Milestone 1 is complete.** 141 tests, all green, none of them touching the network.

- `dossier.store` — SQLite plus migrations, applied on open so there is no separate
  migrate step to forget. `fact.filed_date` is NOT NULL and the primary key includes the
  reporting accession, so a restatement inserts alongside the original.
- `dossier.jobs` — the job table. Content-addressed idempotency keys, output written and
  fsynced before the row flips to `done`, resume as the documented query, and
  `reclaim_stale` for jobs stranded by a session that was cut off.
- `dossier.asof` — the single read path to the `fact` table. All three biases are
  enforced here. A structural test asserts no other module reads that table, and it has
  already caught one violation (the status command).
- `dossier.edgar` — rate-limited client, ten requests a second, mandatory `User-Agent`
  validated before any request goes out.
- `dossier.ingest` — EDGAR JSON into the store, tag-filtered to the ~40 tags the screens
  need, idempotent on re-run.
- `dossier.cli` — `ingest`, `status`, `resume`.
- CI on `ubuntu-latest` and `windows-latest`, Python 3.11 and 3.12.

Verified end to end against the fixtures: FY2023 revenue is invisible on 2023-11-02,
reads 383,285,000,000 from the original 10-K on 2023-11-03, and reads the restated
383,000,000,000 once the FY2024 10-K lands — with both versions still on disk.

## In progress

- Nothing. Milestone 1 is a clean stopping point.

## Next concrete step

**Milestone 4, not milestone 2.** The plan's own build-order note says to get to Pass A
(the risk-factor diff) as early as you can stand to, because it is the cheapest test of
whether the idea works at all. That needs just enough of milestone 2 to extract Item 1A
from two consecutive 10-Ks for one company you know well — not the full 50-filing
tested extractor.

So: write the failing test for Item 1A extraction against a committed 10-K fixture,
make it pass, then wire Pass A over the two extracted sections and read the output. If
it surfaces something you didn't know about a company you already understand, the
project is real.

One thing to decide before then: a live ingest has never been run, because this
environment cannot reach sec.gov. Run `dossier ingest --limit 500` on your own machine
once to confirm the client behaves against the real SEC.

## Outstanding, and only you can do it

**Branch protection on `main` has not been applied yet.** `CLAUDE.md` states the rule
and `ci-green` exists for it to require, but the GitHub setting itself needs repository
admin, which no agent session has. Until it is applied, the branch discipline rests on
instructions rather than on a control. The exact settings are in the session notes; the
short version is: require a pull request, require the `ci-green` check, forbid force
pushes and deletions on `main`.

## Known environment constraints

- **`sec.gov` is unreachable from the Claude Code cloud sessions** used to build this
  (the egress proxy returns 403 on CONNECT). Every test therefore runs against committed
  fixtures and a fake HTTP transport, which is where they belong anyway. A live ingest has
  to be run on a developer machine. Live-network tests are marked `@pytest.mark.network`
  and are deselected by default.
- PyPI is reachable, so `uv` works normally.

## Milestone map

| # | Milestone | Status |
| --- | --- | --- |
| 1 | Job table + EDGAR ingest → SQLite, 500 filers, `filed_date` enforced | **done** (not yet run live) |
| 2 | Section extractor for Item 1A / 7 / footnotes, tested on 50 filings | not started |
| 3 | Five screens as SQL views + JSON output | not started |
| 4 | Pass A (risk-factor diff) end to end, one company | not started |
| 5 | FastAPI app: screen index + dossier + filing diff | not started |
| 6 | Packaging, guided first run, sample database | not started |
| 7 | Passes B, C, D | not started |
| 8 | Valuation engine with bear/base/bull | not started |
| 9 | Thesis generator + bear pass + journal | not started |
| 10 | Quarterly falsification re-check job | not started |

Build-order note from the plan: get to milestone 4 as early as you can stand to. It is the
cheapest possible test of whether the idea works at all. Keep milestone 1 tight rather than
gold-plating the data layer.
