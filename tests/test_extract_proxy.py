"""Extracting a proxy statement, for Pass D.

A DEF 14A has no Item numbers, so the 10-K extractor finds nothing in it. Its sections
are named headings instead, and the names vary: "Compensation Discussion and Analysis"
is near-universal, "Pay Versus Performance" only exists since 2023, and a filer that
puts its summary table under "Executive Compensation Tables" is not hiding it.

What matters for Pass D is the handful of sections that say what management is paid on.
"""

import pytest

from dossier.extract import PROXY_SECTIONS, extract_proxy

PROXY = """
<html><body>
<p>NOTICE OF ANNUAL MEETING OF SHAREHOLDERS</p>
<p>You are invited to attend.</p>
<p>PROPOSAL 1: ELECTION OF DIRECTORS</p>
<p>The board recommends a vote FOR each nominee.</p>
<p>COMPENSATION DISCUSSION AND ANALYSIS</p>
<p>Our annual incentive plan is funded on adjusted operating income, weighted 70%, and
on sales growth, weighted 30%. For fiscal 2026 the adjusted operating income target was
$140 million.</p>
<p>SUMMARY COMPENSATION TABLE</p>
<p>The following table sets out compensation for our named executive officers.</p>
<p>PAY VERSUS PERFORMANCE</p>
<p>Compensation actually paid is reconciled to total shareholder return below.</p>
<p>CERTAIN RELATIONSHIPS AND RELATED PERSON TRANSACTIONS</p>
<p>One director is a partner of a firm that provided legal services.</p>
<p>AUDIT COMMITTEE REPORT</p>
<p>The committee reviewed the financial statements.</p>
</body></html>
"""


class TestExtractProxy:
    def test_it_finds_the_compensation_discussion(self):
        sections = {s.item: s for s in extract_proxy(PROXY)}
        cda = sections["CDA"]
        assert "adjusted operating income, weighted 70%" in cda.text
        assert cda.confidence > 0.5

    def test_a_section_ends_where_the_next_one_starts(self):
        """Running the compensation discussion into the audit committee report would
        hand Pass D a document about two different things."""
        sections = {s.item: s for s in extract_proxy(PROXY)}
        assert "SUMMARY COMPENSATION TABLE" not in sections["CDA"].text
        assert "related person" not in sections["CDA"].text.lower()

    def test_it_finds_the_other_sections_pass_d_reads(self):
        found = {s.item for s in extract_proxy(PROXY)}
        assert {"CDA", "SUMMARY_COMP", "PAY_VS_PERFORMANCE", "RELATED_PERSON"} <= found

    def test_a_missing_section_is_absent_rather_than_empty(self):
        """Pay Versus Performance did not exist before 2023. A zero-length section would
        read as a filer that reported nothing, which is a different claim."""
        without = PROXY.replace("<p>PAY VERSUS PERFORMANCE</p>", "")
        found = {s.item for s in extract_proxy(without)}
        assert "PAY_VS_PERFORMANCE" not in found
        assert "CDA" in found

    def test_a_document_that_is_not_a_proxy_yields_nothing(self):
        assert extract_proxy("<html><body><p>Item 1A. Risk Factors</p></body></html>") == []

    def test_every_section_name_has_a_pattern(self):
        assert set(PROXY_SECTIONS) == {
            "CDA",
            "SUMMARY_COMP",
            "PAY_VS_PERFORMANCE",
            "RELATED_PERSON",
            "DIRECTOR_COMP",
        }

    @pytest.mark.parametrize(
        "heading",
        [
            "Compensation Discussion and Analysis",
            "COMPENSATION DISCUSSION AND ANALYSIS",
            "Compensation Discussion & Analysis",
        ],
    )
    def test_house_styles_of_the_same_heading_all_match(self, heading):
        html = f"<html><body><p>{heading}</p><p>Paid on adjusted EBITDA.</p></body></html>"
        assert [s.item for s in extract_proxy(html)] == ["CDA"]
