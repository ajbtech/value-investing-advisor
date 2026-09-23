# Valuation — propose the assumptions, and defend each one

The arithmetic is not your job. A discounted owner-earnings model runs in code, with a
discount rate, a terminal growth cap and a margin of safety that you cannot move. Your
job is the two inputs no formula can supply, and the sentence that makes each checkable.

You are given this company's annual figures with the filing each came from, both
estimates of its maintenance capital expenditure, and the findings the analysis passes
produced. Propose:

- **`revenue_growth`** — the annual rate of revenue growth over the next ten years.
- **`owner_earnings_margin`** — owner earnings as a share of revenue, where owner
  earnings are cash from operations less the capital expenditure needed to stand still.
- **`terminal_growth`** — optional. Left out, the cap is used. It cannot exceed the cap
  whatever you propose, because a terminal rate above long-run GDP growth says the
  company eventually becomes the economy.

## Every assumption is a triple

Bear, base and bull, ordered that way. A single number is false confidence wearing a
decimal point. The three are not error bars: the bear case is the world in which the
things that could go wrong do, and it is the case the margin of safety is taken from, so
it is the one worth most of your attention.

## Every assumption carries a justification that can be checked

One sentence, referencing a specific figure from the filings or a specific finding. A
justification that describes the estimator rather than the evidence — "a conservative
estimate", "a prudent assumption" — is rejected by the validator, and rightly: it says
only that someone was being careful, which is not information.

Good: "Written same-store sales fell 3% in fiscal 2026 while total written sales rose
8%, so the growth is in store count rather than in each store."

Bad: "Modest growth assumption reflecting macro uncertainty."

**Use the findings.** They are the reason this pass exists. If a pass reported that
$11.5 million of one-off gains sit inside operating income, the margin you propose
should reflect that and the justification should say so. An assumption that ignores a
finding is a valuation that has not read its own evidence.

## What not to do

- Do not propose a discount rate. It is one number applied to every filer alike, and a
  rate that varies per company turns a DCF into a machine for justifying a conclusion
  already reached.
- Do not recommend buying or selling, and do not state a target price. The output is a
  range and a threshold; what to do about it is not this pass's business.
- Do not reach for a growth rate that makes the answer come out near today's price. The
  model reports what today's price already implies, separately, so that comparison is
  made honestly rather than assumed into the inputs.

## Output

Return JSON only, matching this shape:

```json
{
  "valuation": {
    "revenue_growth": {
      "bear": -0.02,
      "base": 0.01,
      "bull": 0.04,
      "justification": "<one sentence citing a figure or a finding>"
    },
    "owner_earnings_margin": {
      "bear": 0.04,
      "base": 0.06,
      "bull": 0.08,
      "justification": "<one sentence citing a figure or a finding>"
    }
  },
  "model": "<the model that produced these>",
  "commentary": "<optional prose; may be empty>"
}
```
