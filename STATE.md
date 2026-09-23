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
- **Kodak, flagged at a 54.4% owner-earnings yield, needed a human — and got one.** Its
  FY2025 operating cash flow is $480M against −$7M in 2024 and a −$128M net loss
  (accession 0001193125-26-104214, filed 2026-03-12). The MD&A pass answered it: the
  section says the increase was "primarily due to cash proceeds received from Reversion
  Assets from KRIP of $618 million". The arithmetic was right and the conclusion it
  invites was wrong, which is the failure mode the analysis layer exists to catch.

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

## The plan, checked against what exists (2026-09-22)

Read the build plan end to end against the code. Most of it is built as written. Six
places diverge, and they are worth stating rather than discovering later.

1. ~~**Pass A is a risk-factor diff, not the risk-factor *and MD&A* diff the plan
   specifies.**~~ — **fixed, see below.**
2. **Survivorship: the schema is ready, the data is not.** `filer.status` carries
   `deregistered` / `delisted_for_cause` / `acquired`, and ingest is careful never to
   reactivate a filer recorded as failed — but nothing ever *sets* a terminal status, and
   the universe is built from `company_tickers.json`, which lists current registrants
   only. A filer that went bankrupt in 2023 is not in the store to be excluded from a
   2022 screen. Until a source of dead CIKs is ingested, every historical screen is
   survivorship-biased, and `--compare 12,24` is the first feature that actually depends
   on this being right.
3. **Bulk ZIPs are still unused.** The plan and `CLAUDE.md` both say to prefer the
   nightly `companyfacts.zip` and `submissions.zip` over per-company API calls; ingest
   makes per-company calls, which is why a 5,000-filer ingest is an hour and a half of
   wall clock at the SEC's rate limit rather than one download. Fine at 500 filers, the
   binding constraint at 5,000.
4. **The extractor was hand-checked on 11 filings, all from one filer.** The plan asks
   for 50 hand-checked filings, and the point of the number is issuer variety: Apple's
   house style is consistent across a decade, so the boundary logic has been tested
   against one way of writing a 10-K. Every extraction bug found so far came from real
   filings, and there are 500 filers in the store to sample.
5. **The financial track does not exist.** Banks, insurers and REITs are excluded by SIC
   (86 of 501 filers last run), and the plan asks for them to be routed to a separate
   track with their own screens rather than merely dropped. Deferred deliberately; noted
   so the exclusion is not mistaken for a decision that they do not matter.
6. **The analysis layer does read the store directly.** The plan's output contract says
   the analysis layer receives the candidate JSON "and nothing else". In practice
   `analyze --prepare` reads `document_section` and the `candidate` row from the store,
   which is what the component table in `CLAUDE.md` describes. The property the plan
   wanted is preserved where it matters — the *prepared input file* is self-contained, so
   a pass is reproducible from it alone — but the stage is not database-free, and saying
   so is cheaper than someone later assuming it.

### Pass A now reads the MD&A too, and it earned its place immediately

Item 1A and Item 7 have a prompt each (`pass_a_v3` and `pass_a_mdna_v1`), the prompt
version travels with every finding and with the run's idempotency key, and an item with
no prompt of its own is refused rather than run against instructions written for another
section — Item 8 is Pass B's job, not Pass A's with the wrong prompt.

**Run live on La-Z-Boy (CIK 57131), FY2026 vs FY2025 Item 7: 11 findings, 0 dropped, 0%
fabrication.** The company was flagged partly on a Piotroski improvement, and the MD&A
says where a good deal of fiscal 2026's operating income came from:

- **Joybird's goodwill was impaired by $20.0 million**, in the reporting unit the prior
  filing described as having "an estimated fair value that exceeds its carrying value by
  approximately 16%" (0000057131-25-000029). Corporate and Other's operating loss widened
  by $37.7 million.
- **The sensitivity analysis behind that cushion is gone.** The prior filing said "using
  a range of reasonable inputs, the fair value of the Joybird reporting unit exceeded its
  carrying value for each of the various scenarios analyzed". The unit it described was
  impaired within the year, and the disclosure is absent from the fiscal 2026 filing.
- **50 basis points of the Wholesale segment's SG&A improvement is a reserve release** —
  "lower warranty expense due to a reduction in our warranty liability driven by a change
  in which we provide external dealers an upfront service allowance" — and **$11.5
  million of gains on sale-leasebacks and a building sale sit inside operating income**,
  against consolidated operating income of $129.2 million (0000057131-26-000019).
- **The dealer count fell from "approximately 1,900 other dealers" to "over 1,000"**, and
  the form changed from an approximation to a floor.
- **The same-store sales definition gained an exclusion** — "and excludes the benefit of
  net new stores and acquired stores" — in a filing reporting total written sales up 8%
  against written same-store sales down 3%.
- The UK manufacturing business closed, the Casegoods wholesale business is being sold,
  the revolver's maturity moved to 2030 and its fixed charge coverage covenant was
  reduced, and expected capex rose to $90–110 million with distribution transformation
  named first.

None of this is visible in Item 1A, and none of it contradicts the Piotroski score — it
says what the score is made of. That is the argument for the pass, tested rather than
asserted.

**Kodak (CIK 31235), FY2025 vs FY2024 Item 7: 8 findings, 0 dropped, 0% fabrication —
and the screener's own anomaly is now answered.** Kodak was flagged at a 54.4% owner
earnings yield on $480M of operating cash flow against −$7M the year before, and
`STATE.md` had parked "how much of that is the pension reversion" as a Pass B question.
Item 7 answers it outright: "Net cash from operating activities increased $487 million
for the year ended December 31, 2025 as compared with the prior year primarily due to
cash proceeds received from Reversion Assets from KRIP of $618 million ... partially
offset by a $153 million payment of excise tax on KRIP reversion asset surplus"
(0001193125-26-104214). The rest of the pass follows the money:

- The surplus settled at **$1.023 billion**, above the prior year's estimated "$750
  million and $900 million" (0000950170-25-040256), but Kodak kept **$144 million in net
  cash** "after required debt payments and payment of excise taxes" — below the prior
  filing's projected "$160 million and $250 million" — plus **$158 million of investment
  assets** that "are primarily hedge fund investments which are in redemption".
- $312 million of the proceeds repaid term loans, $153 million paid the excise tax, and
  a March 2026 amendment "requires the Company to pay $50 million of the Term Loans on or
  before March 18, 2026 and $50 million on or before June 1, 2026, in each case plus a 1%
  prepayment fee".
- In the same filing, the company still describes "plans to return to sustainable
  positive cash flow" — management's own statement that $480 million is not that.

**This is the clearest case yet for the split the whole design rests on.** The screen's
arithmetic was correct and the conclusion it invited was wrong. No prompt caught it and
no ratio could have: one sentence in Item 7 did, and it is quoted, cited and validated.

**Diffing MD&A needs one more step than Item 1A.** A plain sentence diff of the two
sections was 60,684 characters, because most of MD&A is last year's sentences with this
year's numbers in them. Masking digits before diffing separates the language changes from
the numeric-only ones, and the numeric-only list was 15 pairs. The technique is recorded
in the skill; the script is ten lines and worth rewriting rather than carrying.

Two other things the audit found that are now fixed:

- **The screen trend never reached the pass.** `--compare` computes `trend` and
  `history`, `store_candidates` writes both into the candidate payload, and
  `_screen_reason` dropped them on the way to Pass A — so the distinction the plan calls
  for by name ("a company that has been cheap for two years is a different animal") was
  computed, stored, documented in the skill, and then discarded one function before the
  model saw it. Both now travel to the prompt, together: `trend` alone would be a label
  the model cannot check, and Flexsteel's "new" means it was ineligible earlier rather
  than newly cheap. Prompt `pass_a_v3` explains what each trend implies for what to read
  for, and what it does not license — a trend cannot be quoted, so it can never be a
  finding.
- **The README described milestone 1 of 10 and "your own API keys".** Both were false:
  the API-key line contradicted the design decision that there is no key. Rewritten
  against what the code actually does.

## Milestone 8 — the valuation engine, built and run live

`dossier value --cik N --prepare` / `--load`, the same seam as an analysis pass. Owner
earnings discounted over ten years: cash from operations less the capital expenditure
needed to stand still. Operating cash flow is already after interest and tax, so
discounting owner earnings gives the equity value directly and net debt is not
subtracted twice.

**What a model may propose, and what it may not.** Revenue growth and the owner earnings
margin, each as a bear/base/bull triple with a justification. The discount rate (10%),
the terminal growth cap (2.5%) and the margin of safety (30% off the *bear* case) are
constants in `dossier.valuation`. A proposed `discount_rate` is refused by name; a
terminal rate above the cap is clamped rather than argued with; a triple ordered
bear-above-bull is refused, because it would put the margin of safety on the wrong end
of the range. A justification that describes the estimator rather than the evidence — "a
conservative estimate" — is rejected, and one citing a figure or an accession passes.

**Maintenance capex, the input filers do not report,** is estimated two ways — total
capex, and the seven-year average of capex as a share of revenue, applied to this year's
revenue — and both are kept with the spread between them. The lesser is used. For
La-Z-Boy that is $63.2M against $76.3M of actual capex, a $13.1M spread, which is the
size of the estimate's own uncertainty made visible.

**Live on La-Z-Boy, as of 2026-09-23** (revenue $2,126,635 thousand, owner earnings
$140.9M, 40.05M shares, $30.33 a share):

| | per share |
| --- | --- |
| bear (−3% growth, 4% margin) | $19.59 |
| base (+1% growth, 6% margin) | $39.07 |
| bull (+4% growth, 7.5% margin) | $60.69 |
| buy below (30% under bear) | $13.72 |

**Today's price implies revenue growth of −2.6% a year** at the base margin. That number
is why the plan asks for implied expectations: the market is not pricing the base case
and disagreeing with it would be the position, not the starting point. The assumptions
came from the findings — the bear case is the Casegoods wholesale disposal and
same-store sales down 3%; the margin history behind the triple runs 6.6%, 5.9%, 5.1% and
0.1% over the last four years, so the bear margin of 4% is *not* the worst this business
has done.

**What is deliberately not built.** No sensitivity grid, no scenario weighting, no
probability-weighted expected value: a weighted average of three scenarios is a point
estimate with extra steps, and the range is the answer. Milestone 9 (thesis, bear pass,
journal) is where a valuation turns into a decision, and nothing here does that.

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

**Candidates analysed so far: 4 of 30**, two of them (La-Z-Boy and Kodak) over both Item
1A and Item 7. Running totals across all Pass A runs, including Apple: **52 findings, 0
dropped, 0% fabrication**, over seven passes.

Reading both Item 1A sections end to end costs tens of thousands of tokens per company.
Diffing is far cheaper and has found every result so far: compare the risk-factor
*headings* first, and when those are identical — as they were for Armstrong — run a
sentence-level diff (`difflib.SequenceMatcher` over sentences) and read only what moved.
The helper scripts live outside the repo; they are ten lines each and worth rewriting
rather than carrying.

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
- **Armstrong World Industries (7431)** — 7 findings. Flagged on a perfect Piotroski 9/9,
  rank 1 of 195. Its risk-factor headings are identical year over year, so everything is
  inside the paragraphs: two of its largest distributor customers were acquired by the
  national home centres it also sells through ("in September 2025, GMS, Inc. ... was
  acquired by The Home Depot, Inc.", "in October 2025, Foundation Building Materials,
  Inc. ... was acquired by Lowe's Companies, Inc."), "We may pursue strategic
  transactions" became "We are likely to", and limited-source supply became "a limited,
  or single, number of suppliers".
- **Korn Ferry (56679)** — 6 findings. Flagged Magic Formula rank 1 and quality-at-price.
  Its named competitor list gains Eightfold AI, LinkedIn, Paradox and Symphony Talent,
  and it now expects competition "especially" from AI-enabled companies — the company
  naming entrants against the advantage the quality screen is betting on. Two risk
  factors were dropped outright (stockholder activism, bank failures), and the Scope 1
  and 2 emissions goal for 2025 plus the Science-Based Target initiative commitment are
  gone from the corporate-responsibility factor.

**A pattern worth noting across four unrelated filers.** Apple, Kodak and Korn Ferry all
removed or genericised explicit ESG language in the same year — Apple dropped "ESG",
"diversity, equity and inclusion" and "climate change and greenhouse gas emissions",
Kodak renamed its factor from ESG to sustainability and dropped "intensifying", and Korn
Ferry deleted its named emissions target. Pass A found each independently; none of these
is visible in any financial statement.

Next, in rough order of value:

1. **The other 28 candidates.** Each is `dossier extract --cik N` then a Pass A the
   session reads and writes. That is a session's work, not a command, so it is worth
   doing in batches and watching the fabrication rate across them: two companies at 0% is
   a data point, thirty is a metric. Diffing the risk-factor *headings* first is much
   cheaper than reading both sections end to end, and points straight at what moved.
2. ~~Screens as of 12 and 24 months ago, diffed~~ — **built.** `dossier screen --compare
   12,24` runs the screens at each date and gives every candidate a `history` and a
   `trend`. On the live store: 7 persistent, 11 new, 8 returning, 4 recent. La-Z-Boy has
   screened well at all three dates, Armstrong has scored a top Piotroski three years
   running, and Kodak is new — consistent with a one-off pension reversion. Flexsteel's
   "new" is a data fact, not a value signal: it was ineligible at both earlier dates.

   **This found a serious bug.** `first_seen` was being written with the filer's *most
   recent* filing date, and the as-of universe filters on `first_seen <= as_of`, so
   every filer vanished from every past date and a historical screen returned an empty
   universe in silence. 498 of 501 filers were affected. Ingest now records the earliest
   filing it knows of, including from stub filings that predate the submissions window,
   and migration 007 repairs what is already stored from the filings themselves.

   A second lesson from the same run: the screen job's fingerprint counts rows, on the
   assumption that rows are only ever added. A migration *edits* them, so the repair did
   not invalidate the cached run and the old answer kept being served. `SCREENER_VERSION`
   is the lever for that, and it is now 3.
3. ~~Pass A over Item 7~~ — **built and run on La-Z-Boy**, above. The other candidates'
   MD&A is now worth reading alongside their Item 1A, which roughly doubles the work per
   company and, on the evidence of one filer, more than doubles what it finds.
4. **A wider universe.** 500 filers by lowest CIK is not the market. `--limit 5000`
   would take roughly an hour and a half of ingest at the SEC's rate limit — or one
   download, if the bulk ZIPs of audit item 3 land first.
5. ~~Milestone 8, the valuation engine~~ — **built and run live on La-Z-Boy**, above.
   Milestone 9 (thesis, bear pass, journal) is the next milestone proper, and the plan
   calls it the part that compounds for a single user.

The audit's other items — dead CIKs, bulk ingest, a wider extractor sample, the
financial track — are above in "The plan, checked against what exists", with why each
one is not urgent yet and what makes it urgent.

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
| 8 | Valuation engine with bear/base/bull | **done**, run live on La-Z-Boy (CIK 57131) |
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
