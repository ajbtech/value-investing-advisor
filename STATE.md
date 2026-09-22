# STATE.md

Where the build stands. A cold session should be able to read this and start work without
re-deriving the plan. Read `CLAUDE.md` first for the invariants; this file is only status.

The build plan lives outside the repo, as a Claude doc:
<https://claude.ai/artifact/MtKsvd1pve5CEkgcxsXQ47>

## Current milestone

**Milestone 4's gate is passed.** Pass A produced 9 real, quote-validated findings
against real Apple 10-Ks with 0% fabrication. Deciding what's next — see below.

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

**Milestone 4's gate is passed, on a real filer, on a developer machine.** `dossier
ingest`, `dossier extract` and `dossier analyze` have now all run live against
sec.gov (CIK 320193, Apple) rather than only against fixtures — see the session notes
below for what a hand-check of real filings actually turned up, and for the findings
Pass A produced.

**Extraction, hand-checked against 11 real Apple 10-Ks (FY2015–FY2025):** Item 1A's
boundary detection is solid — correct start/end and 1.0 confidence on every one, even
though only synthetic fixtures existed when the extractor was written. Two things a
hand-check caught that the fixtures did not anticipate:

- **A real gap, low severity:** the FY2015 and FY2016 filings leak a running
  page-footer (`"Apple Inc. | 2015 Form 10-K | 17" / "Table of Contents"`) into the
  Item 1A body — ten occurrences in FY2015 alone. The fixtures test a repeated
  *heading* (`Item 1A. Risk Factors (continued)`) but not a bottom-of-page footer, and
  confidence doesn't catch it because it's a content-noise problem, not a boundary
  problem. Apple's later HTML doesn't have this pattern, so it hasn't mattered yet,
  but a filer whose footer noise persists into recent years would still slip through
  silently.
- **Confidence doing its job, not a gap:** Item 1B in the FY2023–FY2025 filings scores
  0.8 because it now ends at Item 1C (Cybersecurity, an item SEC added in 2023) rather
  than Item 2. `EXPECTED_SUCCESSOR` predates that rule change, so the score correctly
  flags something worth a look rather than confidently assuming a stale item map.

**Pass A, run for real:** prepared Apple's FY2025-vs-FY2024 Item 1A pair, read both in
full as the model, and produced 9 findings — **0 dropped, 0% fabrication rate.** Not
boilerplate: the changes include Apple deleting every explicit "ESG" / "diversity,
equity and inclusion" / "climate change and greenhouse gas emissions" phrase from its
stakeholder-expectations risk factor; a dedicated "retail stores" risk factor that no
longer appears at all; a newly dated 2025 U.S.-tariff disclosure with a live Section
232 investigation; a newly dated account of the Google antitrust remedy order; a new
AI-training-data copyright risk; and a softened claim about manufacturing
concentration ("substantially all... a small number of outsourcing partners, often in
single locations" → "a significant majority... in addition to sourcing from... the
U.S."). That is a company-you-know-well result, not confident mush — the gate holds.

**What's actually next:** decide whether Milestone 4's remaining scope (Item 7/MD&A,
not just Item 1A; more than one company) is worth doing before Milestone 3 (the
screens), or whether one clean pass on one company is enough proof and it's time to
move on. Also worth doing at some point, not urgently: fix the running-footer leak in
`dossier.extract.normalise` (a footer pattern is a normalisable-away thing, the same
family as script/style stripping) and update `EXPECTED_SUCCESSOR["1B"]` to accept
`"1C"` without a confidence penalty now that it's a normal, not exceptional, filing
shape.

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

Rebalanced again after the scope change: this is a personal tool whose repository is
public, driven from Claude Code on an existing subscription rather than an API key.

| # | Milestone | Status |
| --- | --- | --- |
| 1 | Job table + EDGAR ingest → SQLite, `filed_date` enforced | **done**, run live on Apple (CIK 320193) |
| 2 | Section extractor for Item 1A / 7 / footnotes | **validated on 11 real Apple 10-Ks** — one known gap, see below |
| 4 | Pass A (risk-factor diff) end to end, one company | **gate passed** — 9 findings, 0% fabrication, on a real filer |
| 3 | Five screens as SQL views + JSON output | after Pass A proves out |
| 8 | Valuation engine with bear/base/bull | not started |
| 9 | Thesis generator + bear pass + journal | **raised** — compounds for a single user |
| 10 | Quarterly falsification re-check job | **raised** — the highest-leverage feature |
| 7 | Passes B, C, D | not started |
| 5 | FastAPI app: screen index + dossier + filing diff | **deferred** |
| 6 | Packaging, guided first run, sample database | **deferred** — no strangers to survive |

**Milestone 4 is the gate.** If Pass A produces confident mush on a company you know
well, none of the rest is worth building. That was true when this was going to be a
product and it is just as true now.

**Why 5 and 6 are deferred.** The plan justified a local web app because its views need
live queries — arbitrary as-of dates, re-run screens at new thresholds, diff any two
filings — and precomputing that is combinatorially hopeless. That argument does not
apply when the front end is a conversation: nothing is precomputed, because Claude
re-runs the CLI on request. The remaining views are documents, and documents render as
artifacts. If this is ever published for other people, milestones 5 and 6 come back.

**Routines.** Milestone 10 (the quarterly falsification re-check) is the one piece of
genuinely scheduled work, and the plan calls it the highest-leverage feature in the
system. It must run where the data is: the store lives in a per-user data directory and
cloud sessions cannot reach `sec.gov`, so a Routine for it has to be bound to the user's
own computer. It is also the one job that justifies the API path in `dossier.models`,
since nobody is present to drive a conversation at the moment it fires.

Build-order note from the plan: get to milestone 4 as early as you can stand to. It is the
cheapest possible test of whether the idea works at all. Keep milestone 1 tight rather than
gold-plating the data layer.
