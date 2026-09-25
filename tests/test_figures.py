"""The per-filer figures, as a read model the screens, the valuation and the re-check share.

Found in review: valuing one company called the screener's `prepare`, which materialises
every filer's facts and builds all five screens. At 12,197 filers that took 93 seconds to
read one company's figures. Annual figures, share count, market cap and eligibility are
each computed from one filer's own data, so they can be built for just the filers asked
for. Only the rankings need the whole universe, and only the screener ranks.
"""

import pytest

from dossier.figures import annual_rows, maintenance_capex, prepare_figures
from dossier.store import open_store
from tests.builders import StoreBuilder
from tests.test_screens import AS_OF, eligible_filer


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "test.sqlite") as conn:
        b = StoreBuilder(conn)
        for cik in (1, 2, 3):
            eligible_filer(b, cik)
        b.done()
        yield conn


def _ciks(conn, table):
    return {row[0] for row in conn.execute(f"SELECT DISTINCT cik FROM temp.{table}")}


class TestScopedToTheFilersAsked:
    @pytest.mark.parametrize(
        "table", ["fact_asof", "price_asof", "universe_asof", "annual", "screen_base"]
    )
    def test_materialises_only_the_filers_named(self, store, table):
        prepare_figures(store, AS_OF, ciks=[2])
        assert _ciks(store, table) == {2}

    def test_everyone_when_no_filers_are_named(self, store):
        prepare_figures(store, AS_OF)
        assert _ciks(store, "screen_base") == {1, 2, 3}

    def test_one_filers_figures_do_not_depend_on_the_others(self, store):
        prepare_figures(store, AS_OF)
        everyone = [dict(r) for r in annual_rows(store, 2)]
        base = dict(store.execute("SELECT * FROM screen_base WHERE cik = 2").fetchone())
        prepare_figures(store, AS_OF, ciks=[2])
        assert [dict(r) for r in annual_rows(store, 2)] == everyone
        assert dict(store.execute("SELECT * FROM screen_base WHERE cik = 2").fetchone()) == base

    def test_the_as_of_rule_still_holds_when_scoped(self, store):
        """Scoping narrows who is read, never when: a figure filed after the date stays
        invisible. FY2022 is filed 2023-02-15, so on 2023-01-01 FY2021 is the latest."""
        prepare_figures(store, "2023-01-01", ciks=[2])
        assert max(row["fy_end"] for row in annual_rows(store, 2)) == "2021-12-31"


class TestMaintenanceCapexIsAFigure:
    def test_lives_in_the_read_model(self):
        rows = [{"fy_end": "2024-12-31", "capex": 60.0, "revenue": 2000.0}]
        assert maintenance_capex(rows)["total_capex"] == 60.0


class TestPerFilerStagesReadOneFiler:
    def test_the_valuation_does_not_materialise_the_whole_store(self, store):
        from dossier.valuation import prepare_valuation

        prepare_valuation(store, cik=2, as_of=AS_OF)
        assert _ciks(store, "fact_asof") == {2}
