"""Reading a run of quarterly form indexes, once, for every command that needs it.

Review finding 4: `dossier deregistrations` and `dossier universe` each carried the same
fifteen-line loop over years and quarters, differing only in the parser they applied.
"""

from dossier.formindex import read_quarters


def test_parses_every_quarter_and_counts_what_each_yielded():
    def fetch(year, quarter):
        return f"{year}Q{quarter}"

    found, quarters = read_quarters(fetch, 2020, 2020, parse=lambda text: [text], label="rows")
    assert found == ["2020Q1", "2020Q2", "2020Q3", "2020Q4"]
    assert quarters[0] == {"year": 2020, "quarter": 1, "rows": 1}


def test_a_quarter_that_cannot_be_fetched_is_reported_and_the_rest_still_read():
    """A quarter that has not happened yet, or a gap in the archive. Neither is a
    reason to lose the quarters that did parse."""

    def fetch(year, quarter):
        if quarter == 2:
            raise RuntimeError("404 Not Found")
        return "x"

    found, quarters = read_quarters(fetch, 2021, 2021, parse=lambda text: [text], label="rows")
    assert len(found) == 3
    assert quarters[1] == {"year": 2021, "quarter": 2, "error": "404 Not Found"}


def test_spans_several_years_in_order():
    seen = []

    def fetch(year, quarter):
        seen.append((year, quarter))
        return ""

    read_quarters(fetch, 2019, 2020, parse=lambda text: [], label="rows")
    assert seen[0] == (2019, 1)
    assert seen[-1] == (2020, 4)
    assert len(seen) == 8
