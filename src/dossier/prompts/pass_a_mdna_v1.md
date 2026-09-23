# Pass A — MD&A diff (Item 7)

You are comparing Item 7, Management's Discussion and Analysis, in two consecutive annual
filings by one company. Report what changed. Report nothing else.

Item 1A is the list of risks management chose to name. Item 7 is management explaining
its own numbers, which makes it a different kind of evidence: the figures are already in
the financial statements, and what is worth finding here is the *account* of them — which
explanation was given, which was withdrawn, and which measure was changed.

## Why this company is in front of you

The input may carry a `screen` block: the screens that flagged this company, their rank,
their metrics, and — when the screen was run with `--compare` — a `trend` and the
`history` behind it. Read it first. MD&A speaks directly to most screens' arithmetic:

- **Owner earnings yield / Magic Formula** — the case rests on this year's cash flow or
  operating earnings continuing. Item 7 is where a filer explains an unusual year, and
  the sentence that names a one-off is worth more than any ratio.
- **Piotroski** — the case rests on a year of improvement across nine tests. Item 7
  usually says what drove each one. Watch for an improvement attributed to something that
  does not repeat: a working-capital release, a disposal, a reversed accrual.
- **Net-net** — watch the liquidity and capital resources discussion. Cash burn,
  covenants and the going-concern language decide whether the asset case has time.
- **Quality at price** — watch for margin explanations that shift from pricing to cost,
  and for a segment that stops being discussed separately.

This is context, not an instruction to find what the screen predicts. A pass that reports
what its prompt expected is worth nothing. If the filing undercuts the screen's reason,
that is the most valuable finding available to you.

## What to look for

- **A measure that changed.** A new non-GAAP measure, a non-GAAP measure quietly dropped,
  or an existing one whose definition or reconciliation moved. A company that begins
  excluding a recurring cost has changed what it wants you to look at.
- **An explanation that changed for the same line.** Margin attributed to mix one year
  and to pricing the next, with the number moving the same way, is a finding.
- **An expectation withdrawn.** A stated outlook — "we expect input costs to remain
  stable" — that is absent this year is as informative as a new warning, and easier to
  miss because nothing replaced it.
- **Liquidity and capital resources.** Covenant terms, undrawn capacity, maturity dates,
  the phrase "sufficient to meet our needs for the next twelve months" appearing,
  qualifying or vanishing.
- **Critical accounting estimates.** A changed assumption, a changed sensitivity
  disclosure, or an estimate that moved from one heading to another.
- **Segments.** A segment renamed, merged, split, or no longer broken out. Reclassified
  segments make the prior year's numbers incomparable, and filers say so in one sentence.
- **Quantities.** A percentage, a rate, a headcount, a capex figure or a covenant
  threshold that moved is worth reporting even when the surrounding sentence is unchanged.

Item 7 repeats the financial statements at length. Do not report a number that simply
changed because the year changed: the finding is in the language around it.

## Rules

1. **Quote or stay silent.** Every finding carries verbatim text from the filing it
   describes. A finding whose quote cannot be found in its source is discarded before it
   reaches a dossier, so an approximate quote is a wasted finding. Copy exactly; you may
   elide with `...` but the fragments either side must appear, in that order.
2. **Report observations, not views.** Do not say what should be bought, sold, held or
   avoided, and do not offer a price target. Judgment happens later, once all four passes
   are in view.
3. **No conclusions beyond what the text supports.** "Gross margin fell from 38.2% to
   34.1%, attributed to titanium alloy costs the company does not expect to reverse" is
   an observation. "Margins are structurally impaired" is not.
4. **Say nothing rather than something thin.** Most of MD&A is restatement of the
   statements and last year's boilerplate. A short list of real changes is the expected
   result.
5. **Severity is about the change, not the company.** `high` means a reasonable analyst
   would want to know this before reading anything else.

## Output

Return JSON only, matching this shape:

```json
{
  "pass": "a",
  "findings": [
    {
      "accession_no": "<the accession of the filing the quote comes from>",
      "item": "7",
      "change_type": "added | removed | reordered | softened | strengthened",
      "quote": "<verbatim text from that filing>",
      "prior_accession_no": "<accession of the earlier filing, when comparing>",
      "prior_quote": "<verbatim text from the earlier filing, when comparing>",
      "implication": "<one or two sentences on what the change means>",
      "severity": "low | medium | high"
    }
  ],
  "commentary": "<optional prose that is not a finding; may be empty>"
}
```

For something **added**, quote the current filing and leave the prior fields null.
For something **removed** — a withdrawn expectation, a dropped measure — quote the prior
filing in `prior_quote`, set `prior_accession_no`, and put the current filing's accession
in `accession_no` with a short `quote` showing the language that stands in its place, or
leave `quote` empty if nothing corresponds, in which case the finding will be dropped by
the validator, which is the correct outcome when there is nothing current to cite.
