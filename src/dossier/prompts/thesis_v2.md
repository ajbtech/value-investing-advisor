# Thesis — what you believe, and what would prove you wrong

You are writing the record of a decision, not a case for one. In two years the only
thing that will still be worth reading is whether you were right for the reasons you
gave, and that is only answerable if the reasons were specific enough to check.

You are given the valuation with its assumptions, and every finding the analysis passes
produced. Write six things.

## 1. The business, in three sentences

What it sells, to whom, and how it makes money. If it cannot be stated simply, that is
itself a finding, and saying so plainly is better than a paragraph that hides it.

## 2. Why it is mispriced

A specific, nameable mechanism: forced selling, a misunderstood segment, a temporary
earnings trough, index exclusion, a spin-off nobody covers, a legal overhang clearing.
**"The market is wrong" is not a reason** — it restates your position. Name what the
market is looking at, and what it is missing, and the validator will reject a
`reason_type` that is only a synonym for "cheap".

## 3. What has to go right

Conditions, not hopes. "Same-store sales stop declining" is a condition. "Consumer
demand recovers" is a hope with no edge attached.

## 4. Falsification conditions

Explicit and measurable, at least two, each with a number. These are the heart of the
document: a quarterly job re-checks them against new filings and tells you when one has
been breached. It cannot check a sentence, so every condition needs a `metric`, a
`direction`, a numeric `threshold` and a `window`.

Good: gross margin below 34% for two consecutive quarters.
Bad: the turnaround fails to materialise.

Write the conditions you would actually act on, not the ones that are easy to satisfy.
A condition nothing could ever breach is a thesis with no way to be wrong.

### Which conditions the re-check can measure by itself

These metric names are computed from XBRL and checked automatically. Write a condition
using one of these names, exactly, and `dossier recheck` will evaluate it every quarter:

`owner_earnings_margin`, `operating_margin`, `gross_margin`, `revenue_growth`,
`revenue`, `operating_income`, `net_income`.

Anything else — same-store sales, store count, backlog, a segment's margin, order
intake — is reported in prose rather than in XBRL, and the re-check will return it as
**needs a human** rather than quietly passing it. That is not a reason to avoid such a
condition: the most informative measure of a business is often the one it only discusses
in words, and a condition that needs reading is still infinitely better than none. It is
a reason to **make at least one condition machine-checkable**, so that a quarter nobody
has time to read still gets checked on something.

## 5. Holding period, and what would make you sell early

A number of years, and the specific event — not "if it goes down".

## 6. Pre-mortem

It is three years later and this lost half its value. What happened? Write the most
plausible version, not the most flattering one.

## Rules

- **No recommendations.** This is a research record and this tool makes no
  individualised recommendations. Say what you believe and what would falsify it; the
  decision belongs to the reader, and the validator rejects text that tells them what to
  do with the stock.
- **Answer to the findings.** If a pass reported that the margin included a reserve
  release, a thesis resting on that margin has to say so. An argument that ignores its
  own evidence is not a thesis, it is a preference.
- **Write it to be read against.** Assume someone — you, later — will check every claim
  against what actually happened.

## Output

Return JSON only:

```json
{
  "thesis": {
    "business": "<three sentences>",
    "mispricing": {
      "reason_type": "<the mechanism, e.g. a temporary earnings trough>",
      "explanation": "<what the market sees, and what it is missing>"
    },
    "must_go_right": ["<condition>", "<condition>"],
    "falsification": [
      {
        "metric": "<what is measured>",
        "direction": "below | above",
        "threshold": 0.34,
        "window": "<e.g. two consecutive quarters>"
      }
    ],
    "holding_period_years": 4,
    "sell_early_if": "<the specific event>",
    "pre_mortem": "<what went wrong, three years on>"
  },
  "model": "<the model that wrote this>"
}
```
