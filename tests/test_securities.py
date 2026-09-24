"""Telling a company's common stock from its other listed securities.

EDGAR lists every security a filer has registered and `ingest` keeps the first one, so a
filer's stored ticker can be a preferred issue or an exchange-listed bond rather than the
common stock. Market cap computed from a preferred share's price is not that company's
market cap, and every ratio built on it is wrong by an unknowable multiple.

At 500 filers no such ticker reached the eligible universe — other filters caught them
first. At 8,000 they will, which is why this exists now rather than later.

The real examples below are from the store: the share classes are common stock, the
`-P` suffixes are preferred.
"""

import pytest

from dossier.securities import classify_ticker, is_common_stock


class TestShareClassesAreStillCommonStock:
    @pytest.mark.parametrize(
        "ticker,name",
        [
            ("BF-B", "BROWN FORMAN CORP"),
            ("TAP-A", "MOLSON COORS BEVERAGE CO"),
            ("CRD-A", "CRAWFORD & CO"),
            ("MOG-A", "MOOG INC."),
            ("AKO-A", "ANDINA BOTTLING CO INC"),
            ("BRK-B", "Berkshire Hathaway"),
        ],
    )
    def test_a_class_suffix_is_common(self, ticker, name):
        assert classify_ticker(ticker) == "common"
        assert is_common_stock(ticker)

    def test_a_plain_ticker_is_common(self):
        assert classify_ticker("LZB") == "common"


class TestTheOtherSecurities:
    @pytest.mark.parametrize(
        "ticker",
        ["SCE-PG", "CMS-PB", "CTA-PB", "CDR-PB", "SNUS-PH", "ALL-PB"],
    )
    def test_a_p_suffix_is_preferred(self, ticker):
        assert classify_ticker(ticker) == "preferred"
        assert not is_common_stock(ticker)

    @pytest.mark.parametrize(
        "ticker,kind",
        [
            ("ABC-WS", "warrant"),
            ("ABC-WT", "warrant"),
            ("ABC-U", "unit"),
            ("ABC-RT", "right"),
            ("ABC-WI", "when_issued"),
        ],
    )
    def test_the_other_suffixes_are_named(self, ticker, kind):
        assert classify_ticker(ticker) == kind
        assert not is_common_stock(ticker)

    def test_a_dot_separator_works_the_same_way(self):
        """Some feeds write BRK.B and SCE.PG for the same securities."""
        assert classify_ticker("BRK.B") == "common"
        assert classify_ticker("SCE.PG") == "preferred"


class TestNotGuessing:
    def test_no_ticker_is_unknown_rather_than_common(self):
        """A filer with no ticker cannot be priced at all, and calling it common would
        invite exactly the market cap this module exists to prevent."""
        assert classify_ticker(None) == "unknown"
        assert classify_ticker("") == "unknown"
        assert not is_common_stock(None)

    def test_an_unrecognised_suffix_is_not_silently_accepted(self):
        assert classify_ticker("ABC-ZZZZ") == "unknown"
        assert not is_common_stock("ABC-ZZZZ")

    def test_case_and_spacing_do_not_decide_it(self):
        assert classify_ticker(" sce-pg ") == "preferred"
        assert classify_ticker("bf-b") == "common"
