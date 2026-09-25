"""Facts from the nightly `companyfacts.zip` instead of one request per filer.

Ingest keeps about forty of the thousands of tags a filer reports and discards the rest,
so widening the tag set used to mean fetching every filer's `companyfacts` again: twelve
thousand requests to add one tag. The SEC publishes all of them as one nightly ZIP, and
CLAUDE.md has always said to prefer it. Kept on disk, it turns a tag change into a local
re-parse.

The ZIP supplies facts only. Who is in the universe still comes from the form index and
the ticker map, and a filer's filings still come from its submissions document.
"""

import json
import zipfile
from pathlib import Path

import httpx
import pytest

from dossier.bulk import companyfacts_version, read_company_facts
from dossier.ingest import ingest_facts
from dossier.store import open_store
from tests.builders import StoreBuilder

FIXTURES = Path(__file__).parent / "fixtures"
APPLE = 320193
VALID_UA = "Jane Doe jane@example.com"


def make_zip(path: Path, docs: dict[int, dict], when=(2026, 9, 24, 3, 0, 0)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for cik, doc in docs.items():
            info = zipfile.ZipInfo(f"CIK{cik:010d}.json", date_time=when)
            zf.writestr(info, json.dumps(doc))
    return path


@pytest.fixture
def apple_facts():
    return json.loads((FIXTURES / "companyfacts_CIK0000320193.json").read_text())


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "test.sqlite") as conn:
        yield conn


class TestReadingTheZip:
    def test_reads_one_filer_by_cik(self, tmp_path, apple_facts):
        path = make_zip(tmp_path / "companyfacts.zip", {APPLE: apple_facts})
        with zipfile.ZipFile(path) as zf:
            assert read_company_facts(zf, APPLE)["entityName"] == apple_facts["entityName"]

    def test_a_filer_not_in_the_zip_has_no_facts_rather_than_an_error(self, tmp_path):
        """The ZIP's equivalent of the API's 404: a registrant with no XBRL at all."""
        path = make_zip(tmp_path / "companyfacts.zip", {})
        with zipfile.ZipFile(path) as zf:
            assert read_company_facts(zf, APPLE) is None

    def test_the_version_is_when_the_sec_built_the_zip(self, tmp_path, apple_facts):
        """Not the local file's mtime, which a copy or a restore would change. A newer
        ZIP is a new version; the same ZIP downloaded twice is not."""
        path = make_zip(tmp_path / "a.zip", {APPLE: apple_facts}, when=(2026, 9, 24, 3, 15, 0))
        assert companyfacts_version(path) == "2026-09-24T03:15:00"


class TestIngestingFactsOnly:
    def test_writes_the_filers_facts(self, store, apple_facts):
        StoreBuilder(store).filer(APPLE).done()
        result = ingest_facts(store, APPLE, apple_facts)
        assert result.facts_inserted > 0
        n = store.execute("SELECT COUNT(*) FROM fact WHERE cik = ?", (APPLE,)).fetchone()[0]
        assert n == result.facts_inserted

    def test_every_fact_keeps_its_filing_date(self, store, apple_facts):
        StoreBuilder(store).filer(APPLE).done()
        ingest_facts(store, APPLE, apple_facts)
        missing = store.execute("SELECT COUNT(*) FROM fact WHERE filed_date IS NULL").fetchone()[0]
        assert missing == 0

    def test_a_second_pass_inserts_nothing(self, store, apple_facts):
        StoreBuilder(store).filer(APPLE).done()
        ingest_facts(store, APPLE, apple_facts)
        assert ingest_facts(store, APPLE, apple_facts).facts_inserted == 0

    def test_facts_citing_unknown_filings_get_stub_filings(self, store, apple_facts):
        """The same rule as per-filer ingest: keep the fact and stub the filing it
        cites, rather than dropping history the submissions window does not reach."""
        StoreBuilder(store).filer(APPLE).done()
        result = ingest_facts(store, APPLE, apple_facts)
        assert result.stub_filings > 0
        orphans = store.execute(
            "SELECT COUNT(*) FROM fact f LEFT JOIN filing g ON g.accession_no = f.accession_no "
            "WHERE g.accession_no IS NULL"
        ).fetchone()[0]
        assert orphans == 0

    def test_only_the_tags_asked_for_are_kept(self, store, apple_facts):
        StoreBuilder(store).filer(APPLE).done()
        ingest_facts(store, APPLE, apple_facts, tags=frozenset({"NetIncomeLoss"}))
        tags = {r[0] for r in store.execute("SELECT DISTINCT tag FROM fact")}
        assert tags == {"NetIncomeLoss"}

    def test_a_filer_the_store_does_not_hold_is_refused(self, store, apple_facts):
        """The ZIP holds every filer on EDGAR. Adding one because it is in there would
        let the facts file decide the universe, which is the form index's job."""
        with pytest.raises(ValueError, match="not in the store"):
            ingest_facts(store, APPLE, apple_facts)

    def test_first_seen_reaches_back_to_the_earliest_fact(self, store, apple_facts):
        StoreBuilder(store).filer(APPLE, first_seen="2099-01-01").done()
        ingest_facts(store, APPLE, apple_facts)
        first_seen = store.execute(
            "SELECT first_seen FROM filer WHERE cik = ?", (APPLE,)
        ).fetchone()[0]
        earliest = store.execute(
            "SELECT MIN(filed_date) FROM fact WHERE cik = ?", (APPLE,)
        ).fetchone()[0]
        assert first_seen == earliest


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DOSSIER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EDGAR_USER_AGENT", VALID_UA)
    return tmp_path


def _seed(data_dir, *ciks):
    with open_store(data_dir / "edgar.sqlite") as conn:
        b = StoreBuilder(conn)
        for cik in ciks:
            b.filer(cik)
        b.done()


def _offline():
    from dossier.edgar import EdgarClient

    def refuse(request):
        raise AssertionError(f"no network expected, got {request.url}")

    return EdgarClient(VALID_UA, transport=httpx.MockTransport(refuse), sleep=lambda _: None)


class TestBulkCommand:
    def test_ingests_from_a_zip_already_on_disk_without_the_network(
        self, data_dir, apple_facts, capsys
    ):
        from dossier.cli import main

        _seed(data_dir, APPLE, 1750)
        make_zip(data_dir / "bulk" / "companyfacts.zip", {APPLE: apple_facts})
        assert main(["ingest", "--bulk", "--json"], client=_offline()) == 0
        payload = json.loads(capsys.readouterr().out)
        by_cik = {r["cik"]: r for r in payload["results"]}
        assert by_cik[APPLE]["status"] == "ingested"
        assert by_cik[1750]["status"] == "no_facts"
        assert payload["failures"] == 0

    def test_the_same_zip_twice_is_cached(self, data_dir, apple_facts, capsys):
        from dossier.cli import main

        _seed(data_dir, APPLE)
        make_zip(data_dir / "bulk" / "companyfacts.zip", {APPLE: apple_facts})
        main(["ingest", "--bulk", "--json"], client=_offline())
        capsys.readouterr()
        main(["ingest", "--bulk", "--json"], client=_offline())
        assert json.loads(capsys.readouterr().out)["results"][0]["status"] == "cached"

    def test_a_newer_zip_is_read_again(self, data_dir, apple_facts, capsys):
        from dossier.cli import main

        _seed(data_dir, APPLE)
        zip_path = data_dir / "bulk" / "companyfacts.zip"
        make_zip(zip_path, {APPLE: apple_facts}, when=(2026, 9, 24, 3, 0, 0))
        main(["ingest", "--bulk", "--json"], client=_offline())
        make_zip(zip_path, {APPLE: apple_facts}, when=(2026, 9, 25, 3, 0, 0))
        capsys.readouterr()
        main(["ingest", "--bulk", "--json"], client=_offline())
        assert json.loads(capsys.readouterr().out)["results"][0]["status"] == "ingested"

    def test_downloads_the_zip_when_it_is_missing(self, data_dir, apple_facts, tmp_path, capsys):
        from dossier.cli import main
        from dossier.edgar import EdgarClient

        _seed(data_dir, APPLE)
        body = make_zip(tmp_path / "served.zip", {APPLE: apple_facts}).read_bytes()
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, content=body)

        client = EdgarClient(VALID_UA, transport=httpx.MockTransport(handler), sleep=lambda _: None)
        assert main(["ingest", "--bulk", "--json"], client=client) == 0
        assert seen == ["https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"]
        assert (data_dir / "bulk" / "companyfacts.zip").exists()

    def test_cik_limits_the_run_to_those_filers(self, data_dir, apple_facts, capsys):
        from dossier.cli import main

        _seed(data_dir, APPLE, 1750)
        make_zip(data_dir / "bulk" / "companyfacts.zip", {APPLE: apple_facts})
        main(["ingest", "--bulk", "--cik", str(APPLE), "--json"], client=_offline())
        results = json.loads(capsys.readouterr().out)["results"]
        assert [r["cik"] for r in results] == [APPLE]

    def test_json_output_is_only_json(self, data_dir, apple_facts, capsys):
        from dossier.cli import main

        _seed(data_dir, APPLE)
        make_zip(data_dir / "bulk" / "companyfacts.zip", {APPLE: apple_facts})
        main(["ingest", "--bulk", "--json"], client=_offline())
        json.loads(capsys.readouterr().out)
