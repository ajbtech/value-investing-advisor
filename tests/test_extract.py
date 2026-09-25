"""10-K section extraction.

The build plan calls this the fiddly part, and it is: item boundaries are inconsistent
across filers and years, and the obvious implementation extracts the table of contents
instead of the section. Every fixture here encodes a hazard that a naive parser falls
into. Extraction confidence is recorded per section so the analysis layer can refuse to
reason over a bad parse rather than confidently analysing a page of nothing.
"""

from pathlib import Path

import pytest

from dossier.extract import Section, extract_sections, normalise

FILINGS = Path(__file__).parent / "fixtures" / "filings"


def filing(name: str) -> str:
    return (FILINGS / f"{name}.html").read_text(encoding="utf-8")


@pytest.fixture
def with_toc():
    return extract_sections(filing("tenk_with_toc"))


class TestNormalise:
    def test_drops_script_and_style(self):
        text = normalise("<style>.x{color:red}</style><p>Item 1. Business</p>")
        assert "color:red" not in text
        assert "Item 1. Business" in text

    def test_unescapes_entities(self):
        assert "—" in normalise("<p>ITEM 1A &#8212; RISK FACTORS</p>")

    def test_turns_non_breaking_spaces_into_spaces(self):
        """`&nbsp;` inside a heading is why a plain `Item 1A` match misses."""
        assert "Item 1A. Risk Factors" in normalise("<p>Item&nbsp;1A.&nbsp;Risk&nbsp;Factors</p>")

    def test_keeps_block_elements_on_separate_lines(self):
        text = normalise("<p>Item 1A. Risk Factors</p><p>We depend on one supplier.</p>")
        assert "Risk Factors\nWe depend" in text

    def test_closes_up_tags_that_split_a_word(self):
        """`Item 1<font>A</font>.` must read as `Item 1A.`, not `Item 1 A .`"""
        assert "Item 1A." in normalise('<b>Item 1<font style="font-weight:700">A</font>.</b>')


class TestFindsTheSectionNotTheTableOfContents:
    """The single most common way this goes wrong."""

    def test_extracts_the_body_of_item_1a(self, with_toc):
        text = with_toc["1A"].text
        assert "SENTINEL_1A_START" in text
        assert "SENTINEL_1A_END" in text

    def test_does_not_return_the_table_of_contents_entry(self, with_toc):
        """The TOC line `Item 1A. Risk Factors 9` also matches a heading pattern. If the
        extractor takes the first match, the section is one line long and says nothing."""
        section = with_toc["1A"]
        assert section.char_count > 200
        assert "Unresolved Staff Comments\nItem 2." not in section.text

    def test_a_cross_reference_in_prose_is_not_a_section_start(self, with_toc):
        """Item 1's text says `see Item 1A below`. That is a mention, not a heading."""
        assert "We design and manufacture industrial widgets" not in with_toc["1A"].text


class TestBoundaries:
    def test_item_1a_stops_at_item_1b(self, with_toc):
        assert "Unresolved Staff Comments" not in with_toc["1A"].text
        assert "Dayton, Ohio" not in with_toc["1A"].text

    def test_item_7_stops_at_item_7a(self, with_toc):
        text = with_toc["7"].text
        assert "SENTINEL_7_START" in text and "SENTINEL_7_END" in text
        assert "commodity price risk" not in text

    def test_item_7a_is_its_own_section(self, with_toc):
        assert "commodity price risk" in with_toc["7A"].text
        assert "index to consolidated financial statements" not in with_toc["7A"].text

    def test_falls_back_to_the_next_item_when_1b_is_absent(self):
        sections = extract_sections(filing("tenk_no_item_1b"))
        assert "SENTINEL_1A_END" in sections["1A"].text
        assert "We lease 212 stores" not in sections["1A"].text


class TestFormatVariation:
    def test_survives_tags_splitting_the_heading(self):
        sections = extract_sections(filing("tenk_split_heading"))
        assert "SENTINEL_1A_START" in sections["1A"].text

    def test_survives_uppercase_and_an_em_dash(self):
        sections = extract_sections(filing("tenk_no_item_1b"))
        assert "SENTINEL_1A_START" in sections["1A"].text


class TestRunningPageHeaders:
    """Many filers repeat `Item 1A. Risk Factors (continued)` at the top of every page.
    Treating those as boundaries truncates the section at the first page break and
    silently discards most of the risk factors — the parse looks successful and is not.
    """

    @pytest.fixture
    def headers(self):
        return extract_sections(filing("tenk_running_headers"))

    def test_spans_across_a_repeated_heading(self, headers):
        text = headers["1A"].text
        assert "SENTINEL_1A_START" in text
        assert "SENTINEL_MIDDLE" in text
        assert "SENTINEL_1A_END" in text

    def test_still_stops_at_the_next_real_item(self, headers):
        assert "Unresolved Staff Comments" not in headers["1A"].text


class TestMissingSections:
    def test_a_smaller_reporting_company_with_no_risk_factors_yields_nothing(self):
        """Not an empty string. A section that is absent must be absent, or the analysis
        layer will happily reason over nothing at all."""
        sections = extract_sections(filing("tenk_no_risk_factors"))
        assert "1A" not in sections

    def test_the_sections_that_do_exist_are_still_returned(self):
        sections = extract_sections(filing("tenk_no_risk_factors"))
        assert "1" in sections


class TestConfidence:
    def test_is_a_probability(self, with_toc):
        for section in with_toc.values():
            assert 0.0 <= section.confidence <= 1.0

    def test_a_clean_parse_scores_high(self, with_toc):
        assert with_toc["1A"].confidence >= 0.8

    def test_a_fallback_boundary_scores_lower_than_a_clean_one(self):
        """1A ending at Item 2 because 1B is missing is a correct answer arrived at less
        certainly, and the score should say so."""
        clean = extract_sections(filing("tenk_with_toc"))["1A"]
        fallback = extract_sections(filing("tenk_no_item_1b"))["1A"]
        assert fallback.confidence < clean.confidence

    def test_a_suspiciously_short_section_scores_low(self):
        html = (
            "<p>PART I</p><p>Item 1A. Risk Factors</p><p>None.</p>"
            "<p>Item 1B. Unresolved Staff Comments</p><p>None.</p>"
        )
        assert extract_sections(html)["1A"].confidence < 0.6

    def test_a_one_word_item_1b_is_not_penalised(self, with_toc):
        """ "None." is what Item 1B says in most 10-Ks. Scoring it as a suspiciously
        short section would make the confidence signal useless on the one item where
        brevity is the norm — which is why the length expectation is per item."""
        assert with_toc["1B"].text == "None."
        assert with_toc["1B"].confidence >= 0.8

    def test_records_which_item_ended_the_section(self, with_toc):
        """Worth storing: an operator debugging a bad parse wants to know where it
        stopped and why, not just that the confidence was low."""
        assert with_toc["1A"].ended_at == "1B"


class TestPageFooters:
    """SEC HTML repeats a running page footer -- "Company | YYYY Form 10-K | N",
    often followed by a stray "Table of Contents" line from the page-jump link -- at
    the bottom of every page. Left in, it reads as part of the risk-factor prose
    rather than the page furniture it actually is."""

    def test_strips_a_running_page_footer(self):
        text = extract_sections(filing("tenk_page_footer"))["1A"].text
        assert "SENTINEL_1A_START" in text
        assert "SENTINEL_1A_END" in text
        assert "Form 10-K" not in text
        assert "Table of Contents" not in text

    def test_does_not_join_the_words_either_side_of_the_footer(self):
        """Stripping the footer must not glue "markets." to "We depend" -- the
        sentence boundary the footer interrupted has to survive its removal."""
        text = extract_sections(filing("tenk_page_footer"))["1A"].text
        assert "marketsWe depend" not in text


class TestPageNumbers:
    """Found live in Kodak's 10-K: a bare page number sits between "without supplementing"
    and "such cash flow from operations", inside one sentence. Left in, an honest quote of
    that sentence fails the validator and the model is recorded as having fabricated it."""

    def test_a_bare_page_number_does_not_split_a_sentence(self):
        text = normalise(
            "<p>Kodak has not consistently generated positive operating cash flows "
            "without supplementing</p><p>10</p><p>such cash flow from operations with "
            "financing and monetization transactions.</p>"
        )
        assert "without supplementing such cash flow from operations" in text

    def test_a_page_number_followed_by_a_table_of_contents_line_is_one_break(self):
        """The shape Kodak's filing actually has: the page number and the stray page-jump
        link both sit between the two halves of the sentence."""
        text = normalise(
            "<p>Kodak has not consistently generated positive operating cash flows "
            "without supplementing</p><p>10</p><p>Table of Contents</p><p>such cash flow "
            "from operations with financing and monetization transactions.</p>"
        )
        assert "without supplementing such cash flow from operations" in text
        assert "Table of Contents" not in text

    def test_a_number_inside_a_line_is_left_alone(self):
        """A table row flattens to one line. Only a line that is nothing but a number is
        page furniture."""
        text = normalise("<p>Net sales 1,043 998</p><p>11</p><p>Gross profit 210</p>")
        assert "Net sales 1,043 998" in text
        assert "Gross profit 210" in text

    def test_a_heading_at_the_top_of_a_page_is_not_joined_to_the_one_before(self):
        """Found live, on 5 of 21 extracted 10-Ks. "ITEM 6. [RESERVED]" ends a page with
        no full stop, and the next page opens "ITEM 7.". The mid-sentence rule is meant
        to fire only when the next line starts lower-case, but it was compiled
        case-insensitively, so it glued the two headings onto one line and Item 7 —
        the whole MD&A — was never found."""
        text = normalise(
            "<p>ITEM 6. [RESERVED]</p><p>36</p><p>ITEM 7. MANAGEMENT&#8217;S DISCUSSION "
            "AND ANALYSIS OF FINANCIAL CONDITION AND RESULTS OF OPERATIONS</p>"
        )
        assert "\nITEM 7. MANAGEMENT" in text

    def test_a_capitalised_line_after_a_page_break_starts_a_new_line(self):
        text = normalise("<p>Liquidity and Capital Resources</p><p>41</p><p>Cash flows</p>")
        assert "Liquidity and Capital Resources\nCash flows" in text

    def test_a_page_number_between_paragraphs_is_dropped(self):
        text = normalise("<p>First paragraph ends here.</p><p>12</p><p>Second begins.</p>")
        assert "\n12\n" not in text
        assert "First paragraph ends here." in text
        assert "Second begins." in text


class TestItemNumberingEvolves:
    """The SEC added Item 1C (Cybersecurity) to Part I in 2023. A filer whose Item 1B
    now ends at Item 1C rather than Item 2 is following a normal, current filing
    shape, not producing a doubtful parse."""

    def test_item_1b_ending_at_1c_is_not_penalised(self):
        section = extract_sections(filing("tenk_item_1c"))["1B"]
        assert section.ended_at == "1C"
        assert section.confidence == 1.0


class TestSelection:
    def test_returns_only_the_requested_items(self, with_toc):
        sections = extract_sections(filing("tenk_with_toc"), items=("1A",))
        assert set(sections) == {"1A"}

    def test_returns_section_objects(self, with_toc):
        assert isinstance(with_toc["1A"], Section)
        assert with_toc["1A"].item == "1A"
        assert with_toc["1A"].char_count == len(with_toc["1A"].text)
