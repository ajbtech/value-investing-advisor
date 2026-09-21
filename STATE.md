# STATE.md

Where the build stands. A cold session should be able to read this and start work without
re-deriving the plan. Read `CLAUDE.md` first for the invariants; this file is only status.

The build plan lives outside the repo, as a Claude doc:
<https://claude.ai/artifact/MtKsvd1pve5CEkgcxsXQ47>

## Current milestone

**Milestone 1 — job table + EDGAR ingest into SQLite, ~500 filers, `filed_date` enforced.**

## Done

- Nothing yet. Scaffolding only: `pyproject.toml` (uv, src layout), the `dossier` package,
  pytest wired up, `CLAUDE.md` invariants, this file.

## In progress

- Milestone 1, starting with the SQLite schema and migrations.

## Next concrete step

Write the failing test for the migration runner and the `fact` table's `filed_date` column,
then make it pass.

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
| 1 | Job table + EDGAR ingest → SQLite, 500 filers, `filed_date` enforced | in progress |
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
