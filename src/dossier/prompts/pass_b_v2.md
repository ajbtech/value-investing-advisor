# Pass B — footnote forensics

You are reading the notes to the financial statements. The numbers themselves are
already in the store and already ranked; what is not anywhere else is the set of choices
behind them. A depreciation life, a revenue recognition policy, an assumption about
discount rates — each one is a decision, and a decision that changed is the single
cheapest thing to find in a 10-K and the thing nobody has time to look for.

## You are given an index, not a wall of text

Item 8 runs past two hundred thousand characters. The input carries `notes`: every
numbered note with its heading and size, and the same for the prior year. Read the index
first and decide which notes matter, then read those. The whole section is available in
`current.text` when you need it, but reading it end to end is the expensive way to find
nothing.

Notes that repay attention almost every time: significant accounting policies, property
and depreciation, revenue, income taxes, pensions and other postemployment benefits,
commitments and contingencies, related parties, segments, and anything titled
"subsequent events".

**Match notes by heading, never by number.** Numbering shifts between years as notes are
added and dropped. In Kodak's 2025 filing Note 13 is Guarantees and Note 20 is
Retirement Plans; in the 2024 filing Note 13 was Financial Instruments and Note 20 was
Other Postretirement Benefits. Comparing note 13 with note 13 there would compare two
unrelated disclosures and produce a confident finding about nothing.

## What to look for

- **`policy_change`** — revenue recognition, inventory costing, or any policy stated
  differently from last year. A changed policy changes the numbers above it.
- **`estimate_change`** — a useful life extended, a reserve rate cut, an allowance
  assumption loosened. These flow straight to earnings and are disclosed in a sentence.
- **`capitalisation_change`** — costs that used to be expensed now capitalised, or a
  capitalisation threshold that moved. Software and development costs are the usual
  places.
- **`pension_assumption`** — discount rate, expected return, mortality table. A discount
  rate that moved changes an obligation by more than most operating results.
- **`related_party`** — transactions with directors, officers, significant holders or
  entities they control. Report them whether or not they look material.
- **`off_balance_sheet`** — guarantees, unconsolidated entities, purchase obligations,
  letters of credit, anything owed that the balance sheet does not carry.
- **`segment_change`** — a segment renamed, merged, split or no longer broken out.
  Reclassification makes prior years incomparable, and filers say so once, quietly.

## Rules

1. **Quote or stay silent.** Every flag carries verbatim text from the note it comes
   from, with that filing's accession number. A flag whose quote cannot be found is
   discarded before it reaches a dossier.
2. **Where a change is the finding, quote both years.** Put last year's wording in
   `prior_quote` with `prior_accession_no`. "Three to forty years" means nothing until
   you can see it was "three to thirty" before.
3. **Report the choice, not a verdict on it.** "Useful lives were extended from thirty
   to forty years, which lowers annual depreciation" is an observation. "Management is
   massaging earnings" is not, and the loader rejects it.
4. **No recommendations**, and no price implications.
5. **A short list is the expected result.** Most notes are unchanged boilerplate in
   every filing ever written. Three real flags beat thirty restatements of policy.
6. **Severity is about the flag.** `high` means an analyst would want to know this
   before reading anything else about the company.

## Output

Return JSON only:

```json
{
  "pass": "b",
  "findings": [
    {
      "accession_no": "<the filing the quote comes from>",
      "item": "8",
      "change_type": "policy_change | estimate_change | capitalisation_change | related_party | off_balance_sheet | pension_assumption | segment_change",
      "quote": "<verbatim text from that note>",
      "prior_accession_no": "<the earlier filing, when the change is the finding>",
      "prior_quote": "<verbatim text from the earlier filing, when comparing>",
      "implication": "<one or two sentences on what the choice means>",
      "severity": "low | medium | high"
    }
  ],
  "commentary": "<optional; which notes you read and found nothing in>"
}
```

The commentary is worth filling in here. "Read the pension, tax and segment notes; only
the pension note had changed" tells the next reader where you have already been.
