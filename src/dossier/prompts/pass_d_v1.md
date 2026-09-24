# Pass D — proxy incentives

What is management actually paid on?

Compensation metrics predict behaviour better than strategy slides do. A chief executive
bonused on adjusted EBITDA growth will produce adjusted EBITDA growth, whatever it costs
elsewhere; one paid on return on invested capital will be reluctant to build a plant.
Neither is a scandal. Both are forecasts, written down by the company, in a document
almost nobody reads.

You are given the proxy's compensation sections: the Compensation Discussion and
Analysis, the summary compensation table, pay versus performance, director compensation
and related-person transactions. Last year's proxy is beside it where the store has one.

## What to look for

- **`metric_mix`** — what the plan pays on, as it stands. Report this even when nothing
  changed: it is the answer to the question this pass exists to ask. A company paid on
  absolute sales rather than same-store sales is being paid to add stores, whatever
  happens inside the ones it has, and that is worth knowing whether or not it is new.
- **`metric_change`** — a performance metric added, dropped or reweighted. A company
  that stops paying on margin and starts paying on revenue growth has told you what it
  will do next.
- **`target_change`** — the threshold, target or maximum moved. A target reset below
  last year's actual result is a specific and checkable observation.
- **`adjustment`** — a metric defined to exclude something: restructuring, impairment,
  stock compensation, an acquisition's costs. What is excluded from the measure someone
  is paid on is the most informative list in the document.
- **`discretion`** — the committee exercising judgment: a payout above formula, a waived
  condition, an award granted outside the plan, a metric result "as adjusted by the
  committee".
- **`vesting_change`** — a performance period shortened, a retention grant made, a
  cliff changed. Short vesting and long-dated strategy do not sit together.
- **`related_person`** — transactions with directors, officers or their firms.
- **`ownership`** — a guideline changed, a pledging or hedging policy loosened, a holding
  requirement waived.

**Read the weights, and quote them.** "Adjusted operating income, weighted 70%, and
sales growth, weighted 30%" is the whole pass in one sentence when it changes.

## Rules

1. **Quote or stay silent.** Verbatim text from the section you name, with the proxy's
   accession number. Anything unfound is dropped before it is stored.
2. **Quote both years where the change is the finding.** A weighting means little until
   the reader can see what it was.
3. **Report the incentive, not a verdict on the people.** "The annual bonus moved from
   operating margin to sales growth" is an observation. "Management is empire-building"
   is not, and the loader rejects it.
4. **Say nothing about pay levels being high or low.** That is a different argument and
   not this tool's business. What the pay is *conditioned on* is the finding.
5. **No recommendations**, as everywhere else.
6. **A short list is the expected result.** Most proxies repeat last year's plan exactly,
   and reporting that plainly in `commentary` is a real result.

## Output

Return JSON only:

```json
{
  "pass": "d",
  "findings": [
    {
      "accession_no": "<the proxy the quote comes from>",
      "item": "CDA | SUMMARY_COMP | PAY_VS_PERFORMANCE | DIRECTOR_COMP | RELATED_PERSON",
      "change_type": "metric_change | target_change | adjustment | discretion | vesting_change | related_person | ownership",
      "quote": "<verbatim text from that section>",
      "prior_accession_no": "<last year's proxy, when comparing>",
      "prior_quote": "<verbatim text from it>",
      "implication": "<one or two sentences on what behaviour this pays for>",
      "severity": "low | medium | high"
    }
  ],
  "commentary": "<optional; what was unchanged from last year>"
}
```
