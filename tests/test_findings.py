"""Findings and the quote validator.

The first non-negotiable: no uncited claims. Every finding carries verbatim text from
the filing plus the accession number, and a finding whose quote cannot be found in its
source is dropped before it reaches a dossier. A claim that cannot be traced is a bug,
not a nuance.

The validator sits on a knife edge. Too strict and legitimate quotes fail on a curly
apostrophe or a line break, the fabrication rate reads as noise, and the signal becomes
useless. Too loose and it waves through text the filing never contained, which is worse:
it lends false assurance to exactly the failure it exists to catch.
"""

import pytest

from dossier.findings import (
    MIN_QUOTE_CHARS,
    Finding,
    fabrication_rate,
    normalise_for_match,
    quote_appears_in,
    validate_findings,
)

SOURCE = (
    "Our business is concentrated: three customers accounted for 62% of revenue in "
    "the year, up from 51% in the prior year. The loss of any one of them would\n"
    "materially harm our results of operations.\n"
    "We depend on a single supplier for the titanium alloy used in our primary "
    "product line, and we have not qualified a second source."
)


def finding(**overrides) -> Finding:
    defaults = dict(
        accession_no="0000320193-24-000123",
        item="1A",
        change_type="added",
        quote="three customers accounted for 62% of revenue",
        implication="Customer concentration increased materially year over year.",
        severity="high",
    )
    defaults.update(overrides)
    return Finding(**defaults)


class TestNormalisation:
    """Filings are full of typographic noise a model will not reproduce byte for byte."""

    def test_collapses_whitespace_and_line_breaks(self):
        assert normalise_for_match("would\nmaterially  harm") == "would materially harm"

    def test_unifies_curly_quotes(self):
        assert normalise_for_match("the company’s") == normalise_for_match("the company's")

    def test_unifies_dashes(self):
        assert normalise_for_match("2024–2025") == normalise_for_match("2024-2025")

    def test_ignores_case(self):
        """A model excerpting mid-sentence often capitalises the first word. That is not
        fabrication, and treating it as such would bury the real signal in noise."""
        assert normalise_for_match("Three Customers") == normalise_for_match("three customers")


class TestQuoteMatching:
    def test_accepts_a_verbatim_quote(self):
        assert quote_appears_in("three customers accounted for 62% of revenue", SOURCE)

    def test_accepts_a_quote_spanning_a_line_break(self):
        assert quote_appears_in("them would materially harm our results", SOURCE)

    def test_rejects_text_the_filing_does_not_contain(self):
        assert not quote_appears_in("four customers accounted for 81% of revenue", SOURCE)

    def test_rejects_a_plausible_paraphrase(self):
        """The whole point. A paraphrase is exactly what fabrication looks like."""
        assert not quote_appears_in("customer concentration rose sharply this year", SOURCE)

    def test_rejects_a_number_that_was_altered(self):
        """Changing 62% to 64% is the most dangerous possible failure: it reads as
        sourced, survives a skim, and is wrong."""
        assert not quote_appears_in("three customers accounted for 64% of revenue", SOURCE)

    def test_rejects_a_quote_too_short_to_be_evidence(self):
        """`revenue` appears in every filing ever written. A match that trivial proves
        nothing and would inflate the pass rate into meaninglessness."""
        assert not quote_appears_in("revenue", SOURCE)
        assert len("revenue") < MIN_QUOTE_CHARS

    def test_rejects_an_empty_quote(self):
        assert not quote_appears_in("", SOURCE)


class TestElidedQuotes:
    """Models legitimately quote with an ellipsis. Rejecting that outright would fail
    honest findings; accepting it blindly would let two unrelated fragments be stitched
    into a claim the filing never made. Every fragment must appear, in order."""

    def test_accepts_an_ellipsis_whose_fragments_appear_in_order(self):
        assert quote_appears_in(
            "three customers accounted for 62% of revenue ... The loss of any one of them",
            SOURCE,
        )

    def test_accepts_a_unicode_ellipsis(self):
        assert quote_appears_in(
            "three customers accounted for 62% of revenue … loss of any one of them",
            SOURCE,
        )

    def test_rejects_fragments_that_appear_out_of_order(self):
        """Reversing them asserts a sequence the filing does not contain."""
        assert not quote_appears_in(
            "The loss of any one of them ... three customers accounted for 62%", SOURCE
        )

    def test_rejects_when_one_fragment_is_invented(self):
        assert not quote_appears_in(
            "three customers accounted for 62% of revenue ... and we expect this to worsen",
            SOURCE,
        )

    def test_rejects_fragments_too_short_to_be_evidence(self):
        assert not quote_appears_in("Our ... the ... we", SOURCE)


class TestValidateFindings:
    def test_keeps_a_finding_whose_quote_verifies(self):
        kept, dropped = validate_findings([finding()], {"0000320193-24-000123": SOURCE})
        assert len(kept) == 1
        assert dropped == []

    def test_drops_a_finding_whose_quote_does_not(self):
        bad = finding(quote="four customers accounted for 81% of revenue")
        kept, dropped = validate_findings([bad], {"0000320193-24-000123": SOURCE})
        assert kept == []
        assert len(dropped) == 1
        assert dropped[0].reason == "quote_not_found"

    def test_drops_a_finding_citing_a_filing_we_do_not_have(self):
        """Not a pass. An unverifiable citation is indistinguishable from a fabricated
        one, and treating it as valid is how an unsourced claim reaches a dossier."""
        orphan = finding(accession_no="0000000000-00-000000")
        kept, dropped = validate_findings([orphan], {"0000320193-24-000123": SOURCE})
        assert kept == []
        assert dropped[0].reason == "source_unavailable"

    def test_validates_the_prior_quote_of_a_diff_too(self):
        """Pass A compares two filings. A finding is only as good as both its ends."""
        diff = finding(
            change_type="softened",
            prior_accession_no="0000320193-23-000106",
            prior_quote="language that was never in the earlier filing",
        )
        kept, dropped = validate_findings(
            [diff],
            {"0000320193-24-000123": SOURCE, "0000320193-23-000106": SOURCE},
        )
        assert kept == []
        assert dropped[0].reason == "prior_quote_not_found"

    def test_keeps_a_diff_whose_both_ends_verify(self):
        diff = finding(
            change_type="softened",
            prior_accession_no="0000320193-23-000106",
            prior_quote="we have not qualified a second source",
        )
        kept, _ = validate_findings(
            [diff],
            {"0000320193-24-000123": SOURCE, "0000320193-23-000106": SOURCE},
        )
        assert len(kept) == 1

    def test_drops_a_finding_with_no_quote_at_all(self):
        kept, dropped = validate_findings([finding(quote="")], {"0000320193-24-000123": SOURCE})
        assert kept == []
        assert dropped[0].reason == "quote_missing"

    def test_reports_the_finding_alongside_the_reason(self):
        """An operator tuning a prompt needs to see what was rejected, not just a count."""
        bad = finding(quote="invented text that is certainly long enough to count")
        _, dropped = validate_findings([bad], {"0000320193-24-000123": SOURCE})
        assert dropped[0].finding.implication == bad.implication


class TestFabricationRate:
    """A pipeline whose fabrication rate you cannot state is one you cannot trust."""

    def test_is_zero_when_everything_verifies(self):
        assert fabrication_rate(kept=10, dropped=0) == 0.0

    def test_is_one_when_nothing_does(self):
        assert fabrication_rate(kept=0, dropped=7) == 1.0

    def test_is_the_proportion_dropped(self):
        assert fabrication_rate(kept=9, dropped=1) == pytest.approx(0.1)

    def test_is_none_when_there_is_nothing_to_measure(self):
        """Zero findings is not a zero fabrication rate. Reporting 0.0 there would read
        as a clean bill of health for a pass that produced nothing at all."""
        assert fabrication_rate(kept=0, dropped=0) is None


class TestFindingShape:
    def test_rejects_an_unknown_change_type(self):
        with pytest.raises(ValueError):
            finding(change_type="vibes")

    def test_rejects_an_unknown_severity(self):
        with pytest.raises(ValueError):
            finding(severity="catastrophic")

    def test_round_trips_through_json(self):
        original = finding()
        assert Finding.from_dict(original.to_dict()) == original

    @pytest.mark.parametrize(
        "implication",
        [
            "We should buy this stock before the next earnings call.",
            "Investors should avoid the shares until the backlog stabilises.",
            "We recommend a buy at current levels.",
            "Our price target is $84.",
            "The shares are worth buying on this weakness.",
        ],
    )
    def test_rejects_a_buy_or_sell_conclusion(self, implication):
        """These passes report observations, not views. Judgment happens later, once all
        four passes are in view — a pass that recommends is a pass that skipped that."""
        with pytest.raises(ValueError):
            finding(implication=implication)

    @pytest.mark.parametrize(
        "implication",
        [
            "The company plans to sell its European division within twelve months.",
            "Management disclosed it may buy back shares under the existing authority.",
            "The filing adds that customers can avoid the surcharge by prepaying.",
            "A covenant requires the company to hold minimum liquidity of $200 million.",
        ],
    )
    def test_does_not_reject_an_ordinary_observation_that_mentions_trading_words(self, implication):
        """The guard matches phrases, not bare words. Banning "sell" outright would
        reject a plain statement about a divestiture — and a guard that fires on honest
        findings gets switched off, which leaves nothing guarding anything."""
        assert finding(implication=implication).implication == implication
