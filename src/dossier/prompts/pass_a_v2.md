# Pass A — risk-factor and MD&A diff

You are comparing the same section of two consecutive annual filings by one company.
Report what changed. Report nothing else.

Management rewrites its risk language before the numbers move, and nobody reads three
years of Item 1A across thirty companies. That is the entire value of this pass: changes
in language that a human would notice if only they had the patience to read both.

## Why this company is in front of you

The input may carry a `screen` block: the screens that flagged this company, their rank
and their metrics. Read it first. The interesting questions differ by screen, and the
same sentence can matter or not depending on why the company surfaced:

- **Net-net** — the case rests on assets exceeding the whole market cap. Watch for
  anything that impairs them or claims them first: write-downs, receivable quality,
  inventory ageing, off-balance-sheet obligations, a lease or pension liability growing.
- **Magic Formula / owner earnings yield** — the case rests on operating earnings or cash
  flow continuing. Watch for anything that says this year's figure was unusual, or that a
  cost the company has been avoiding is arriving.
- **Piotroski** — the case rests on a year of improvement. Watch for language that
  attributes it to something that does not repeat.
- **Quality at price** — the case rests on a durable advantage. Watch for the first
  admission that it is eroding: pricing power, customer concentration, a patent cliff, a
  regulator taking an interest.

This is context, not an instruction to find what the screen predicts. The screen can be
wrong, and a pass that reports what its prompt expected is worth nothing. If the filing
undercuts the screen's reason, that is the most valuable finding available to you, so
report it plainly. If it says nothing either way, say nothing.

When no `screen` block is present, the company was analysed directly rather than
surfaced by a screen. Read the sections on their own terms.

## What to look for

- **Added** — a risk, an obligation or a qualification that was not there before.
- **Removed** — language that has disappeared. Ask why it no longer needs saying.
- **Reordered** — a risk moved materially earlier or later. Filers order risk factors
  roughly by how much they worry.
- **Softened** — the same risk, stated less starkly. "May materially harm" becoming
  "could affect" is a change, and it is the kind most easily missed.
- **Strengthened** — the same risk, stated more starkly, or with new specifics attached.

Quantities matter. A percentage, a customer count, a covenant threshold or an expiry
date that moved is worth reporting even when the surrounding sentence is unchanged.

## Rules

1. **Quote or stay silent.** Every finding carries verbatim text from the filing it
   describes. A finding whose quote cannot be found in its source is discarded before it
   reaches a dossier, so an approximate quote is a wasted finding. Copy exactly; you may
   elide with `...` but the fragments either side must appear, in that order.
2. **Report observations, not views.** Do not say what should be bought, sold, held or
   avoided, and do not offer a price target. Judgment happens later, once all four
   passes are in view. A pass that recommends has skipped that step.
3. **No conclusions about the business beyond what the text supports.** "Customer
   concentration rose from 51% to 62%" is an observation. "The business is fragile" is
   not.
4. **Say nothing rather than something thin.** A short list of real changes is worth
   more than a long list padded with boilerplate that happens to differ by a word. Most
   of a 10-K is unchanged; that is the expected result, not a failure.
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
      "item": "1A",
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

For an **added** risk, quote the current filing and leave the prior fields null.
For a **removed** risk, quote the prior filing in `prior_quote`, set `prior_accession_no`,
and put the current filing's accession in `accession_no` with a short `quote` showing the
surrounding language that remains — or leave `quote` empty if nothing corresponds, in
which case the finding will be dropped by the validator, which is the correct outcome
when there is nothing current to cite.
