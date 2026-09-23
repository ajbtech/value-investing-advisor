# STATE.md

Where the build stands. A cold session should be able to read this and start work without
re-deriving the plan. Read `CLAUDE.md` first for the invariants; this file is only status.

The build plan lives outside the repo, as a Claude doc:
<https://claude.ai/artifact/MtKsvd1pve5CEkgcxsXQ47>

## Current milestone

**Milestone 3 is built and has run live over 500 filers.** The five screens produced 30
candidates as of 2026-09-22, and hand-checking them found and fixed two real share-count
bugs. Milestone 4's gate was passed earlier: Pass A produced 9 quote-validated findings
against real Apple 10-Ks with 0% fabrication.

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

## Milestone 3, and what the first live screen run taught

`dossier prices` and `dossier screen` are in. 500 filers ingested, 494 priced, screened
as of 2026-09-22: **289 of 501 filers eligible, 30 candidates**, every exclusion counted
by reason (86 financials, 69 under the $300M floor, 43 without a recent 10-K).

- **Prices** come from Yahoo's keyless chart endpoint into a `price` table, read only
  through the as-of gateway, which a second structural test now enforces. Free price
  sources adjust history for later splits, so a pre-split close is a price that already
  knows about a split that had not happened; `dossier.prices` undoes it from the split
  events in the same response. Stooq was tried first and is now behind a JavaScript
  proof-of-work wall.
- **The screener is SQL** over TEMP tables the gateway materialises, so no screen reads
  `fact` or `price` directly. The five screens stay separate and are never blended.
- **Two share-count bugs the live run exposed, both now fixed with tests.** Company
  facts omit anything reported per share class, so a multi-class filer's cover-page
  count can be one class (HEICO: 55M of 139M, which made it look five times cheaper
  than it is and put it through quality-at-price) or years stale (A. O. Smith's last
  single count is from 2015, which showed a 16.8% earnings yield instead of 9.0%). 15
  of 274 eligible filers were affected. The count is now taken from the cover page only
  if it is under 15 months old and at least 80% of the year's weighted-average basic
  shares, and otherwise from that weighted average.
- **A 404 from companyfacts is not a failure.** Ten of the 500 file no XBRL at all; they
  used to fail and sit in the resume queue retrying what cannot succeed.
- **Performance.** The shared screen tables are materialised once per run rather than
  recomputed per screen: a run went from about two minutes to fifteen seconds.

### Known limitations, in the open

- **Ranked screens flag relative to whoever is in the store.** Magic Formula and owner
  earnings flag their top ten, so their flags mean less in a thin universe. Candidates
  carry `rank` and `ranked` so this is visible. Piotroski, net-net and quality-at-price
  flag on absolute thresholds.
- **Greenblatt's return on capital explodes for asset-light filers.** Korn Ferry shows
  1869% because its tangible capital is tiny after negative working capital. That is the
  definition working as written, but it distorts the combined rank.
- **A filer's ticker may not be its common stock.** EDGAR lists every security, and
  ingest keeps the first, so a preferred issue (`SCE-PG`) or an exchange-listed bond
  (`EAI`) can stand in for the common. None reached the eligible universe this run,
  because other filters caught them first. Nothing detects it yet.
- **Multi-class filers are priced off one class.** The share count covers every class,
  but the price is whichever ticker EDGAR listed first, and HEICO's two classes trade
  about 20% apart.
- **The 500 are the lowest CIKs in the ticker map**, which are the oldest registrants:
  useful for testing, not a representative market.
- **Kodak, flagged at a 54.4% owner-earnings yield, needs a human.** Its FY2025 operating
  cash flow is $480M against −$7M in 2024 and a −$128M net loss (accession
  0001193125-26-104214, filed 2026-03-12). The arithmetic is right; what drove it is a
  question for Pass B, not for the screener.

## Milestone 4, and what its live run taught

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

Both gaps that run exposed are since fixed: the running page-footer is stripped in
`dossier.extract.normalise`, and `EXPECTED_SUCCESSOR` accepts Item 1C after Item 1B.

## Next concrete step

**The pipeline runs end to end, on a company nobody chose by hand.** `analyze --prepare`
now carries the screen reason (prompt `pass_a_v2`), and the first candidate through the
whole chain — screen, extract, prepare, analyse, validate — was La-Z-Boy (CIK 57131),
flagged by three screens. Pass A produced **7 findings, 0 dropped, 0% fabrication**, and
the screen context earned its place: LZB was flagged partly on a Piotroski improvement,
and the prior-year filing that improvement is measured against says "During fiscal 2025
we fully impaired the goodwill and intangible asset related to our businesses in the
United Kingdom" (0000057131-25-000029). In the fiscal 2026 filing the UK is gone from
Item 1A entirely, and the manufacturing list reads "the United States and Mexico".

**Candidates analysed so far: 2 of 30.** Running totals across all Pass A runs, including
Apple: **20 findings, 0 dropped, 0% fabrication.**

- **La-Z-Boy (57131)** — 7 findings. Flagged partly on a Piotroski improvement, and the
  prior-year filing that improvement is measured against records a full impairment of the
  UK goodwill and intangibles. The UK has since left Item 1A entirely.
- **Kodak (31235)** — 4 findings, and the screener's own anomaly answered. Flagged at a
  54.4% owner-earnings yield on $480M of operating cash flow against $-7M the year
  before. The filing now says Kodak "has not consistently generated positive operating
  cash flows without supplementing such cash flow from operations with financing and
  monetization transactions, **such as the KRIP reversion**" — the clause naming the
  reversion is new this year. The prior filing carried a dedicated risk factor on
  receiving reversion proceeds from terminating the Kodak Retirement Income Plan, approved
  by the Board on 2025-01-21; that factor is gone and KRIP now appears once. How much of
  the $480M is reversion is a cash flow statement question, so it belongs to Pass B.
  Item 1A was also cut from 94,649 to 67,337 characters and lost its Summary of Risk
  Factors section, and its ESG risk factor was renamed to sustainability with
  "intensifying" dropped — the same scrubbing Apple's FY2025 filing showed.

Next, in rough order of value:

1. **The other 28 candidates.** Each is `dossier extract --cik N` then a Pass A the
   session reads and writes. That is a session's work, not a command, so it is worth
   doing in batches and watching the fabrication rate across them: two companies at 0% is
   a data point, thirty is a metric. Diffing the risk-factor *headings* first is much
   cheaper than reading both sections end to end, and points straight at what moved.
2. **Screens as of 12 and 24 months ago, diffed** — the plan's "deliberate addition",
   and unbuilt. A company that has been getting cheaper for two years is a different
   animal from one that fell in this quarter, and the difference routes to different
   prompts.
3. **A wider universe.** 500 filers by lowest CIK is not the market. `--limit 5000`
   would take roughly an hour and a half of ingest at the SEC's rate limit.
4. **Milestone 8, the valuation engine**, which is the next milestone proper.

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
| 3 | Five screens as SQL views + JSON output | **done**, run live over 500 filers |
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
