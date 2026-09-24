---
name: dossier
description: Drive the dossier value-investing pipeline from a conversation — ingest SEC filings, check what is in the store, resume interrupted work, and present results. Use whenever the user asks about screening filers, a company's fundamentals as of a date, what the store holds, or wants a dossier, screen or valuation run or explained. Also use when they ask to resume, retry or check on pipeline work.
---

# Driving the dossier pipeline

This repository is a research pipeline that reads SEC filings and produces cited
value-investing dossiers. The CLI is the interface you drive; the conversation is the
interface the user sees. Read `CLAUDE.md` for the invariants before changing anything.

## The one rule that matters most here

**Never characterise a finding you have not read, and never paraphrase a number.**

The pipeline validates every quoted claim against its source filing before it enters a
dossier. That validator protects the dossier's contents. It does not protect your
summary of them. If you restate a figure loosely, or describe what a filing "suggests"
without quoting it, you have added an unvalidated layer to a system built specifically
to have none.

So: quote, cite the accession number, and give the filing URL. When you are unsure
whether the store actually contains something, run the command and look rather than
inferring from context. "I don't have that yet" is always a better answer than a
plausible one.

## Before anything touches EDGAR

The SEC requires a declared `User-Agent` naming a human and an email:

```
export EDGAR_USER_AGENT="Jane Doe jane@example.com"
```

Without it the CLI exits 2 with an explanation, before any request goes out. If you hit
that, tell the user to set it rather than trying to work around it.

**Cloud sessions cannot reach `sec.gov`.** The egress proxy returns 403 on CONNECT. Any
command that fetches from EDGAR will fail in a cloud session — it has to run on the
user's own machine. Everything that only reads the local store works fine anywhere.

## Commands

Every command takes `--json` and emits parseable output on stdout with nothing else.
**Always pass `--json`** — the human-readable form is for the user's own terminal.

| Command | What it does |
| --- | --- |
| `dossier ingest --cik N --json` | Fetch one filer from EDGAR into the store. Repeatable. |
| `dossier ingest --limit N --json` | Fetch the first N filers from the ticker map. |
| `dossier ingest ... --force --json` | Re-run filers already ingested. |
| `dossier extract --cik N --json` | Pull Item sections out of that filer's 10-Ks. |
| `dossier extract --accession A --json` | Extract one named filing. |
| `dossier extract ... --force --json` | Re-extract filings already done. |
| `dossier prices --cik N --json` | Fetch daily closes for an ingested filer. Repeatable. |
| `dossier prices --all --json` | Price every ingested filer that has a ticker. |
| `dossier screen --as-of YYYY-MM-DD --json` | Run the five screens with only what was knowable on that date. |
| `dossier screen ... --out candidates.json` | Also write the candidate array alone: the analysis layer's input. |
| `dossier screen ... --compare 12,24` | Also screen 12 and 24 months earlier and record what changed. |
| `dossier analyze --pass a --cik N --prepare` | Write Pass A's input: the prompt and both Item 1A sections. |
| `dossier analyze --pass a --cik N --item 7 --prepare` | The same, for the MD&A. Item 1A and Item 7 have a prompt each. |
| `dossier analyze --pass b --cik N --prepare` | Pass B: the footnotes (Item 8), split into notes with an index. |
| `dossier extract --cik N --form "DEF 14A"` | Extract the proxy's compensation sections. |
| `dossier analyze --pass d --cik N --prepare` | Pass D: what management is paid on, from the proxy. |
| `dossier analyze --pass a --cik N --load F` | Read findings back, validate every quote, store what survives. |
| `dossier value --cik N --prepare` | Write the valuation's input: the figures, the findings, the fixed rules. |
| `dossier value --cik N --load F` | Read assumptions back, validate every justification, store the range. |
| `dossier thesis --cik N --prepare` | Write the thesis input: the valuation and every finding. |
| `dossier thesis --cik N --load F` | Validate the thesis, append it, write the journal entry. |
| `dossier thesis --cik N --bear --prepare` / `--load F` | The bear pass: attack the stored thesis, quoting filings. |
| `dossier thesis --cik N --pass-over "REASON"` | Record a candidate that cleared screening and was passed over. |
| `dossier recheck --json` | Re-check every open thesis against its own falsification conditions. |
| `dossier status --json` | What the store holds and what work is pending. |
| `dossier resume --json` | Retry everything pending or failed. |

`extract` and `prices` need filers already ingested — stages talk through the store,
never by calling each other, so `ingest` comes first. `screen` needs both facts and
prices: a filer with no close within ten days of the as-of date cannot be screened.

Run them with `uv run dossier ...` so the project's own environment is used.

Exit codes: `0` success, `1` some work failed (the JSON says which), `2` a usage or
configuration problem (the message is on stderr).

### Reading the output

`ingest` and `resume` return a `results` array, one entry per filer, each with a
`status` of `ingested`, `cached`, `failed` or `skipped`. A `cached` result is not a
no-op to apologise for — it means the work was already done and correctly skipped.
`failed` entries stay resumable; suggest `dossier resume` rather than re-running ingest.

`extract` returns the same shape per filing, plus `items`, `sections` and
`lowest_confidence`. Two statuses are specific to it:

- `no_document` — a stub filing with no document URL, created during ingest for an
  accession outside the submissions window. Not a failure, and deliberately not
  resumable: there is nothing to retry until ingest supplies a URL.
- `extracted` with a low `lowest_confidence` — the parse succeeded but something about
  it is doubtful. Check `extraction_confidence`, `heading` and `ended_at` on the
  `document_section` row before using that section for anything.

`prices` returns one result per filer with a `status` of `fetched`, `cached` (already
fetched today), `no_ticker`, `not_ingested` or `failed`. Prices come from Yahoo's
keyless chart endpoint, which is unofficial: a `failed` result naming "delisted" or
"No data found" usually means the ticker no longer trades there.

`screen` returns the whole run:

- `universe` — how many filers were in the as-of universe, how many were `eligible`,
  and `excluded` counted by reason (no recent 10-K, financial track, under five years of
  history, no recent price, no share count, under the $300M floor).
- `screens` — for each of the five: how many filers were `eligible`, how many it could
  `ranked`, how many it `flagged`, and `missing_data` counting the eligible filers it
  could not rank because a figure it needs was not reported. **Report `ranked` against
  `eligible`, not against the flag count.** The remaining difference is the screen's own
  definition — net-net skips a filer whose net current assets are negative,
  quality-at-price one without seven years of returns — and that is the screen working,
  not a hole in the data.
- `candidates` — each with `flagged_by` (screen, `rank`, `ranked` out of how many, and
  the metrics), a `flag_reason`, the `price` and `shares` used for market cap with their
  sources, and `inputs`: every reported fact behind the ratios with its `accession_no`
  and `filed_date`.

With `--compare`, each candidate also carries `history` (what the screens said on each
earlier date, including why it was excluded if it was) and a `trend`:

- `persistent` — flagged at every date. Cheap and staying cheap, which is a different
  animal from a company that just fell in.
- `new` — flagged now and at no earlier date. Check `history` before reading anything
  into it: a filer that was *ineligible* earlier (no price, below the floor) is new to
  the data, not newly cheap.
- `returning` — flagged two years ago, not last year, flagged again now.
- `recent` — flagged last year and now, but not two years ago.

Say which one when presenting a candidate. "Cheap for two years" and "cheap since last
quarter" lead to different questions, and the analysis prompt is told the difference.

**Read `rank` against `ranked`.** Magic Formula and owner earnings flag their top ten
*relative to whatever else is in the store*. "Rank 1 of 1" in a store holding one
filer means nothing; say so rather than presenting it as a finding. Piotroski, net-net
and quality-at-price flag on absolute thresholds, so their flags hold in any universe.

When presenting a candidate, cite the inputs behind any figure you quote: the
accession number and filing, as for any other claim. The screens never blend into one
score, and neither should a summary of them — which screen surfaced a company is the
first thing that matters about it.

**Extraction confidence is not decoration.** 10-K item boundaries are inconsistent, and
a bad parse looks exactly like a good one until you read it. Anything below about 0.6 on
Item 1A should be looked at rather than analysed. Confidence is scored per item, so a
five-character Item 1B is fine — "None." is what most filings say — while a
five-hundred-character Item 1A is not.

## Running Pass A — you are the model

There is no API key. Pass A runs in two halves with you in the middle:

1. `dossier analyze --pass a --cik N --prepare --out input.json` — this hands you the
   prompt and both Item 1A sections. If the screener flagged this company, the input
   also carries a `screen` block: which screens, their rank and their metrics, and —
   when that run used `--compare` — the `trend` and the `history` behind it. Read it
   first — the questions worth asking differ for a net-net and a quality compounder.
   It is context, not a brief: if the filing undercuts the screen's reason, that is the
   most valuable thing you can report, and a pass that finds what its prompt expected
   is worth nothing.
2. **Follow that prompt exactly.** Read both sections, report what changed, and quote
   verbatim from the filing each finding describes. Write the findings to a file in the
   shape the prompt specifies.
3. `dossier analyze --pass a --cik N --load findings.json` — every quote is
   string-matched against its source filing. Anything that does not match is dropped.

**Run it over Item 7 as well.** `--item 7` pairs the MD&A and loads a prompt written for
it; the version travels with the finding, so the two passes stay distinguishable. Item 1A
is the risks management chose to name, Item 7 is management explaining its own numbers,
and the second is where a screen's arithmetic gets its account: an unusual year, an
improvement attributed to a reserve change, a measure whose definition moved. An item
with no prompt of its own — the footnotes, which are Pass B's job — is refused rather
than run against instructions written for another section.

Diffing MD&A needs one more step than Item 1A. Most of it is last year's sentences with
this year's numbers in them, so mask the digits before diffing sentences: what survives
is the language that changed, and the numeric-only differences are worth a separate,
much shorter look. On La-Z-Boy that turned 60,000 characters of raw diff into the
handful of paragraphs that actually moved.

**Expect to be caught, and do not work around it.** If a finding is dropped as
`quote_not_found`, the quote was not in the filing — re-read and quote exactly, rather
than loosening the quote until it passes. The exit code is 1 when anything was dropped,
and the fabrication rate is reported; that number is the point of the whole design.
Report it to the user honestly, including when it is bad.

Never write a finding that recommends buying or selling. The loader rejects those, but
the reason they are rejected matters more than the check: these passes report
observations, and judgment happens later once all four are in view.

## Running Pass B — the footnotes

`dossier analyze --pass b --cik N --prepare` defaults to Item 8 and hands you an **index**
rather than one wall of text: every numbered note with its heading and size, for this
year and last. Item 8 runs past 200,000 characters, so read the index, pick the notes
that matter, and read those from `current.text`.

**Match notes by heading, never by number.** Numbering shifts year to year — in Kodak's
2025 filing Note 13 is Guarantees, in 2024 it was Financial Instruments.

The flags are their own vocabulary, not Pass A's: `policy_change`, `estimate_change`,
`capitalisation_change`, `related_party`, `off_balance_sheet`, `pension_assumption`,
`segment_change`. Calling a depreciation-life extension an `estimate_change` rather than
"softened" is what makes a list of flags worth reading. Everything else is as Pass A:
quote verbatim, cite the accession, and the validator drops what it cannot find.

Fill in `commentary` with which notes you read and found nothing in. It tells the next
reader where you have already been.

## Running Pass D — what management is paid on

The proxies are already in the store (`ingest` keeps every form), but they need
extracting first: `dossier extract --cik N --form "DEF 14A"`. A proxy has no Item
numbers, so its sections are found by name — `CDA`, `SUMMARY_COMP`,
`PAY_VS_PERFORMANCE`, `RELATED_PERSON`, `DIRECTOR_COMP` — and a section the filer titled
something else is absent rather than empty.

Then `dossier analyze --pass d --cik N --prepare` hands over this year's compensation
sections and last year's. **Report the metric mix even when nothing changed** — that is
the question the pass exists to answer, and `metric_mix` is the flag for it. The others
are `metric_change`, `target_change`, `adjustment`, `discretion`, `vesting_change`,
`related_person`, `ownership`.

The useful finding is usually the gap between what the company says it is doing and what
it pays for. La-Z-Boy's plan pays on absolute sales while its same-store sales fall, and
no metric measures return on the capital spent acquiring stores. Say that plainly, with
the quote — and say nothing about whether the pay is too high, which is a different
argument and not this tool's business.

## Running a valuation — you propose, the code decides

Same two halves. `dossier value --cik N --prepare` hands you the company's annual
figures with the filing behind each one, both maintenance capex estimates and the spread
between them, every stored finding, and the constants you cannot change. You return two
triples:

- **`revenue_growth`** and **`owner_earnings_margin`**, each as `bear`, `base`, `bull`
  with a one-sentence `justification`. `terminal_growth` is optional and capped anyway.
- A justification that describes the estimator rather than the evidence — "a
  conservative estimate" — is **rejected by the validator**, and rightly. Cite a figure
  or a finding, so a reader can check it.
- Do not propose a discount rate. It is one number applied to every filer, and the
  loader refuses it by name.

**Use the findings, or the pass has not read its own evidence.** If Pass A reported a
reserve release inside an improving margin, the margin you propose should reflect it and
say so.

When presenting the result: give the **range**, never the base case alone, and say what
today's price already implies — that is often the more useful number, because it turns
"what is it worth" into "what does the market believe, and do I disagree". `buy_below`
is a mechanical 30% discount to the *bear* case; it is a threshold, not a target and not
a recommendation. A valuation is an argument with its assumptions attached, so quote the
justifications when you quote the numbers.

## Writing a thesis, and then attacking it

`dossier thesis --cik N --prepare` needs a stored valuation: the thesis argues from a
price, rather than reaching for one. It hands you the valuation with its assumptions and
every finding, and asks for six sections. Three are enforced in code, so write them
properly rather than discovering the refusal:

- **The mispricing needs a mechanism.** A `reason_type` that is a synonym for "cheap" is
  rejected, and an explanation containing "the market is wrong" is rejected by name.
  Name what the market is looking at and what it is missing.
- **At least two falsification conditions, each with a numeric `threshold`, a
  `direction` and a `window`.** The quarterly re-check reads these; it cannot read a
  sentence. Write the ones you would act on, not the ones easy to satisfy.
- **No recommendations**, in any section.

Revising appends a new version; the old one stays. Then `--bear --prepare` hands the
thesis to the bear pass, whose points are quote-validated exactly like findings: a point
whose quote is not in the cited section is dropped and counted, and the exit code is 1
when anything was. **Report that number** — an unfalsifiable bear case is worth no more
than an unfalsifiable bull one.

Every load writes a journal entry to the user's journal directory, one file each,
never inside the repository. Use `--pass-over "reason"` for a candidate that cleared
screening and was not taken: those entries are what make the journal a record of the
process rather than of its successes.

When presenting a thesis, give the bear case alongside it, always. It is attached to the
thesis permanently and quoting only the bull half misrepresents what the store holds.

## The quarterly re-check

`dossier recheck --json` reads every open thesis against the conditions it set. Each
condition comes back as `breached`, `holding` or **`needs_a_human`**, and the third is
the one to lead with: it means the metric is not in XBRL — same-store sales, store
count, backlog — so nothing was measured. Never round that up to "holding". Exit code is
1 if anything was breached *or* could not be checked.

`recheck` computes `owner_earnings_margin`, `operating_margin`, `gross_margin`,
`revenue_growth`, `revenue`, `operating_income` and `net_income`. When helping write a
thesis, use at least one of those names exactly, so a quarter nobody reads still gets
checked on something.

Report a breach as what it is: the user said they would treat this as evidence they were
wrong. Quote the condition and the measured numbers, and ask whether they still mean it.
Do not suggest selling — that was never this tool's call.

## Workflows

**"What's in the store?"** — `dossier status --json`, then report filers, filings, facts
and any resumable jobs. If `resumable` is above zero, say so and offer to resume.

**"Ingest Apple"** — CIKs are what the CLI takes, not tickers. Apple is 320193. If the
user gives a ticker you are not certain of, ingest by CIK only once you have confirmed
it; do not guess a CIK.

**"What's cheap right now?" / "Run the screens"** — `dossier screen --json` (as of
today) after `ingest` and `prices` have run. Report the universe size and exclusions
first, then each candidate's `flag_reason`, citing inputs for any number you repeat.
Never present a candidate as something to buy: it is a company worth reading about,
which is what the analysis passes are for.

**"Look into the ones that screened well"** — `dossier screen --json`, then for each
candidate worth the tokens: `dossier extract --cik N --json`, then Pass A as below.
Extraction comes first because the analysis reads `document_section`, not filings.

**"What would the screens have said last year?"** — `dossier screen --as-of
YYYY-MM-DD --json`. Everything is filtered to what was knowable on that date, prices
included.

**"Something died halfway"** — `dossier resume --json`. The job table makes this safe:
work already completed is skipped, and an interrupted job loses at most one filer.

**"What was knowable on date X?"** — this is the heart of the system. All fact reads go
through `dossier.asof.AsOfView`, which filters `filed_date <= as_of`. There is no CLI
command for ad-hoc queries yet, so use a short Python snippet against the store, and
never read the `fact` table by any other path — a test enforces that and will fail.

## Presenting results

Prefer a short answer in chat. Reach for an artifact when the output is a document the
user will want to keep or re-read — a dossier, a screen with its reasons, a valuation
with its assumptions. Those carry citations, and citations survive better on a page than
in a scrollback.

When you render one, every factual claim keeps its accession number and filing URL. A
claim whose citation you cannot produce does not go in.

## What not to do

- Do not add price charts, watchlists, alerts, or anything real-time. They are out of
  scope permanently; `CLAUDE.md` explains why.
- Do not present pipeline output as a recommendation to buy or sell. This is a research
  tool and makes no individualised suggestions.
- Do not run a large `--limit` ingest without telling the user roughly what it will cost
  in time and requests. The SEC limit is 10 requests a second.
