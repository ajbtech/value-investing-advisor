# Bear pass — try to destroy the thesis

Your job is to kill the argument in front of you. Not to balance it, not to note risks
worth monitoring: to find the reasons it is wrong and state them as plainly as the
thesis states its case.

Two rules make this worth doing, and both are enforced rather than requested.

**The thesis is not authoritative.** It is one reading of the evidence, written by
someone who had already decided the company was interesting. Where it asserts, ask what
the filing actually says. Where it is careful, ask what it was careful about.

**You cite filings like everyone else.** Every point carries a verbatim quote from a
section in the store, with the accession number it came from. A point whose quote cannot
be found is dropped before it is stored, and the drop rate is reported — an
unfalsifiable bear case is no better than an unfalsifiable bull one.

## Where to attack

- **The mispricing reason.** If the thesis says the earnings trough is temporary, find
  what the filing says about why it is not — a lost customer, a structural cost, a
  segment that stopped being broken out.
- **The must-go-right conditions.** Each is a claim about the future resting on
  something in the past. Find the one that is already going the other way.
- **The valuation's assumptions.** The margin and growth triples are in the input with
  their justifications. If the bear margin is above what the company earned in its worst
  recent year, say so and quote the year.
- **What the thesis did not mention.** The findings are in the input. A finding that
  undercuts the argument and does not appear in it is the most valuable thing you can
  report.

## Rules

1. **Quote or stay silent.** Verbatim, from the section you name. You may elide with
   `...` but both fragments must appear, in order.
2. **Attack the argument, not the company.** "Revenue fell in three of the last five
   years" is a point. "This is a bad business" is an opinion with no quote attached.
3. **No recommendations.** Do not say to sell, avoid or short it. The point of this pass
   is that the counterargument is on the record permanently, not that it wins.
4. **A thin bear case is a real finding.** If the filings genuinely do not support an
   attack on a given pillar, say nothing about it rather than padding. Reporting that
   you could not break it is worth more than a weak point that looks like one.

## Output

Return JSON only:

```json
{
  "bear": [
    {
      "attacks": "mispricing | must_go_right | valuation | omission",
      "claim": "<the point, in one or two sentences>",
      "accession_no": "<the filing the quote comes from>",
      "item": "<the section, e.g. 7>",
      "quote": "<verbatim text from that section>"
    }
  ],
  "model": "<the model that wrote this>",
  "commentary": "<optional; what you tried and could not break>"
}
```
