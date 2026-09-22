"""The command line.

Each stage is independently runnable, so a session that dies mid-run loses one
filing's worth of work and the next session's first command picks up where it stopped.
"""

import json
from pathlib import Path

import httpx
import pytest

from dossier.cli import main
from dossier.store import open_store

FIXTURES = Path(__file__).parent / "fixtures"
VALID_UA = "Jane Doe jane@example.com"


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DOSSIER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EDGAR_USER_AGENT", VALID_UA)
    return tmp_path


@pytest.fixture
def edgar():
    """A fake EDGAR that serves the committed fixtures."""
    submissions = (FIXTURES / "submissions_CIK0000320193.json").read_text()
    facts = (FIXTURES / "companyfacts_CIK0000320193.json").read_text()

    as_json = {"content-type": "application/json"}

    def handler(request):
        url = str(request.url)
        if "/submissions/" in url:
            return httpx.Response(200, text=submissions, headers=as_json)
        if "/companyfacts/" in url:
            return httpx.Response(200, text=facts, headers=as_json)
        if "company_tickers.json" in url:
            return httpx.Response(200, json={"0": {"cik_str": 320193, "ticker": "AAPL"}})
        return httpx.Response(404)

    from dossier.edgar import EdgarClient

    return EdgarClient(VALID_UA, transport=httpx.MockTransport(handler), sleep=lambda _: None)


@pytest.fixture
def yahoo():
    """A fake price source that serves the committed Apple chart fixture."""
    from dossier.prices import YahooPrices

    chart = (FIXTURES / "yahoo_chart_AAPL_2020_split.json").read_text()

    def handler(request):
        if request.url.path.endswith("/AAPL"):
            return httpx.Response(200, text=chart, headers={"content-type": "application/json"})
        return httpx.Response(
            404, json={"chart": {"result": None, "error": {"description": "No data found"}}}
        )

    return YahooPrices(transport=httpx.MockTransport(handler), sleep=lambda _: None)


class TestPricesCommand:
    def test_fetches_closes_for_an_ingested_filer(self, data_dir, edgar, yahoo, capsys):
        main(["ingest", "--cik", "320193"], client=edgar)
        capsys.readouterr()
        assert main(["prices", "--cik", "320193", "--json"], prices=yahoo) == 0
        payload = json.loads(capsys.readouterr().out)
        result = payload["results"][0]
        assert result["status"] == "fetched"
        assert result["ticker"] == "AAPL"
        assert result["days_inserted"] == 10
        with open_store(data_dir / "edgar.sqlite") as conn:
            assert conn.execute("SELECT COUNT(*) FROM price").fetchone()[0] == 10

    def test_a_second_run_the_same_day_is_cached(self, data_dir, edgar, yahoo, capsys):
        main(["ingest", "--cik", "320193"], client=edgar)
        main(["prices", "--cik", "320193", "--json"], prices=yahoo)
        capsys.readouterr()
        main(["prices", "--cik", "320193", "--json"], prices=yahoo)
        assert json.loads(capsys.readouterr().out)["results"][0]["status"] == "cached"

    def test_a_filer_not_in_the_store_is_reported_not_fetched(self, data_dir, yahoo, capsys):
        """Prices hang off a filer row. Ingest comes first, as it does for every stage."""
        assert main(["prices", "--cik", "320193", "--json"], prices=yahoo) == 1
        result = json.loads(capsys.readouterr().out)["results"][0]
        assert result["status"] == "not_ingested"

    def test_a_source_error_fails_that_filer_only(self, data_dir, edgar, yahoo, capsys):
        main(["ingest", "--cik", "320193"], client=edgar)
        with open_store(data_dir / "edgar.sqlite") as conn:
            conn.execute("UPDATE filer SET ticker = 'GONE' WHERE cik = 320193")
            conn.commit()
        capsys.readouterr()
        assert main(["prices", "--cik", "320193", "--json"], prices=yahoo) == 1
        result = json.loads(capsys.readouterr().out)["results"][0]
        assert result["status"] == "failed"
        assert "No data found" in result["error"]

    def test_json_output_is_only_json(self, data_dir, edgar, yahoo, capsys):
        main(["ingest", "--cik", "320193"], client=edgar)
        capsys.readouterr()
        main(["prices", "--cik", "320193", "--json"], prices=yahoo)
        json.loads(capsys.readouterr().out)


class TestHelp:
    def test_bare_invocation_explains_itself(self, capsys):
        assert main([]) == 2
        assert "ingest" in capsys.readouterr().err

    def test_lists_the_stage_commands(self, capsys):
        with pytest.raises(SystemExit):
            main(["--help"])
        assert "ingest" in capsys.readouterr().out


class TestIngestCommand:
    def test_ingests_a_filer(self, data_dir, edgar, capsys):
        assert main(["ingest", "--cik", "320193"], client=edgar) == 0
        with open_store(data_dir / "edgar.sqlite") as conn:
            assert conn.execute("SELECT name FROM filer").fetchone()["name"] == "Apple Inc."

    def test_reports_what_it_did(self, data_dir, edgar, capsys):
        main(["ingest", "--cik", "320193"], client=edgar)
        out = capsys.readouterr().out
        assert "320193" in out

    def test_a_second_run_skips_work_already_done(self, data_dir, edgar, capsys):
        """Check before spending: the second run should be a cache hit, not a re-fetch."""
        main(["ingest", "--cik", "320193"], client=edgar)
        capsys.readouterr()
        main(["ingest", "--cik", "320193"], client=edgar)
        assert "cached" in capsys.readouterr().out.lower()

    def test_force_re_runs_a_completed_job(self, data_dir, edgar, capsys):
        main(["ingest", "--cik", "320193"], client=edgar)
        capsys.readouterr()
        main(["ingest", "--cik", "320193", "--force"], client=edgar)
        assert "cached" not in capsys.readouterr().out.lower()

    def test_records_a_job_per_filer(self, data_dir, edgar):
        main(["ingest", "--cik", "320193"], client=edgar)
        with open_store(data_dir / "edgar.sqlite") as conn:
            row = conn.execute("SELECT job_type, status FROM job").fetchone()
        assert row["job_type"] == "ingest_filer"
        assert row["status"] == "done"

    def test_a_failing_filer_does_not_abort_the_batch(self, data_dir, capsys):
        """One bad filer in a 500-filer run must not cost the other 499."""
        from dossier.edgar import EdgarClient

        good = (FIXTURES / "submissions_CIK0000320193.json").read_text()
        as_json = {"content-type": "application/json"}

        def handler(request):
            if "0000000001" in str(request.url):
                return httpx.Response(500)
            if "/submissions/" in str(request.url):
                return httpx.Response(200, text=good, headers=as_json)
            return httpx.Response(200, json={"cik": 320193, "facts": {}})

        client = EdgarClient(
            VALID_UA, transport=httpx.MockTransport(handler), sleep=lambda _: None, max_retries=1
        )
        exit_code = main(["ingest", "--cik", "1", "--cik", "320193"], client=client)

        out = capsys.readouterr().out
        assert exit_code == 1  # something failed, and the exit code says so
        assert "320193" in out
        with open_store(data_dir / "edgar.sqlite") as conn:
            assert conn.execute("SELECT COUNT(*) FROM filer").fetchone()[0] == 1

    def test_a_failed_job_is_left_resumable(self, data_dir, capsys):
        from dossier.edgar import EdgarClient

        client = EdgarClient(
            VALID_UA,
            transport=httpx.MockTransport(lambda request: httpx.Response(500)),
            sleep=lambda _: None,
            max_retries=1,
        )
        main(["ingest", "--cik", "1"], client=client)
        with open_store(data_dir / "edgar.sqlite") as conn:
            row = conn.execute("SELECT status, attempts, error FROM job").fetchone()
        assert row["status"] == "failed"
        assert row["attempts"] == 1
        assert row["error"]

    def test_limit_caps_the_universe(self, data_dir, edgar, capsys):
        assert main(["ingest", "--limit", "1"], client=edgar) == 0
        with open_store(data_dir / "edgar.sqlite") as conn:
            assert conn.execute("SELECT COUNT(*) FROM filer").fetchone()[0] == 1


class TestUserAgentFailure:
    def test_explains_itself_rather_than_traceback(self, data_dir, monkeypatch, capsys):
        """Getting the User-Agent wrong is the most likely first-run failure. It should
        read as an instruction, not as a stack trace."""
        monkeypatch.delenv("EDGAR_USER_AGENT", raising=False)
        assert main(["ingest", "--cik", "320193"]) == 2
        err = capsys.readouterr().err
        assert "EDGAR_USER_AGENT" in err
        assert "Traceback" not in err


class TestExtractCommand:
    """`dossier extract` reads the filing table and writes document_section. It needs a
    filing already ingested, which is the component boundary doing its job: stages talk
    through the store, never by calling each other."""

    @pytest.fixture
    def edgar_with_filings(self):
        """EDGAR serving both the submissions/facts JSON and a 10-K document."""
        from dossier.edgar import EdgarClient

        submissions = (FIXTURES / "submissions_CIK0000320193.json").read_text()
        facts = (FIXTURES / "companyfacts_CIK0000320193.json").read_text()
        tenk = (FIXTURES / "filings" / "tenk_with_toc.html").read_text()
        as_json = {"content-type": "application/json"}

        def handler(request):
            url = str(request.url)
            if "/submissions/" in url:
                return httpx.Response(200, text=submissions, headers=as_json)
            if "/companyfacts/" in url:
                return httpx.Response(200, text=facts, headers=as_json)
            if url.endswith(".htm"):
                return httpx.Response(200, text=tenk, headers={"content-type": "text/html"})
            return httpx.Response(404)

        return EdgarClient(VALID_UA, transport=httpx.MockTransport(handler), sleep=lambda _: None)

    @pytest.fixture
    def ingested(self, data_dir, edgar_with_filings, capsys):
        main(["ingest", "--cik", "320193"], client=edgar_with_filings)
        capsys.readouterr()
        return data_dir

    def test_extracts_the_filers_ten_ks(self, ingested, edgar_with_filings, capsys):
        assert main(["extract", "--cik", "320193", "--json"], client=edgar_with_filings) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "extract"
        statuses = {r["accession"]: r["status"] for r in payload["results"]}
        # Addressed by accession rather than by position: the filer's stub filing sorts
        # alongside the real ones, and a positional assertion would be testing the sort.
        assert statuses["0000320193-24-000123"] == "extracted"
        assert statuses["0000320193-23-000106"] == "extracted"

    def test_writes_document_sections(self, ingested, edgar_with_filings, capsys):
        main(["extract", "--cik", "320193", "--json"], client=edgar_with_filings)
        with open_store(ingested / "edgar.sqlite") as conn:
            items = {row["item"] for row in conn.execute("SELECT item FROM document_section")}
        assert "1A" in items

    def test_only_touches_ten_ks(self, ingested, edgar_with_filings, capsys):
        """The 10-Q in the fixture has no Item 1A worth diffing; Pass A reads 10-Ks."""
        main(["extract", "--cik", "320193", "--json"], client=edgar_with_filings)
        with open_store(ingested / "edgar.sqlite") as conn:
            forms = {
                row["form_type"]
                for row in conn.execute(
                    "SELECT DISTINCT f.form_type FROM filing f "
                    "JOIN document_section d ON d.accession_no = f.accession_no"
                )
            }
        assert forms == {"10-K"}

    def test_a_second_run_is_cached(self, ingested, edgar_with_filings, capsys):
        main(["extract", "--cik", "320193", "--json"], client=edgar_with_filings)
        capsys.readouterr()
        main(["extract", "--cik", "320193", "--json"], client=edgar_with_filings)
        statuses = {
            r["accession"]: r["status"] for r in json.loads(capsys.readouterr().out)["results"]
        }
        assert statuses["0000320193-24-000123"] == "cached"

    def test_reports_the_weakest_section(self, ingested, edgar_with_filings, capsys):
        """So a filing worth checking by hand is visible without a second query."""
        main(["extract", "--cik", "320193", "--json"], client=edgar_with_filings)
        results = {r["accession"]: r for r in json.loads(capsys.readouterr().out)["results"]}
        result = results["0000320193-24-000123"]
        assert 0.0 <= result["lowest_confidence"] <= 1.0
        assert "1A" in result["items"]

    def test_a_single_accession_can_be_named(self, ingested, edgar_with_filings, capsys):
        main(
            ["extract", "--accession", "0000320193-24-000123", "--json"],
            client=edgar_with_filings,
        )
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["results"]) == 1

    def test_says_so_when_there_is_nothing_to_extract(self, data_dir, edgar, capsys):
        assert main(["extract", "--cik", "999", "--json"], client=edgar) == 0
        assert json.loads(capsys.readouterr().out)["results"] == []

    def test_a_filing_with_no_document_url_is_reported_but_not_a_failure(
        self, ingested, edgar_with_filings, capsys
    ):
        """Stub filings, created during ingest for accessions outside the submissions
        window, have no document to fetch. Reporting them matters — passing over them
        silently would leave a gap nobody notices — but they are not failures: a failed
        job would sit in the resume queue forever retrying what cannot succeed until
        ingest supplies a URL."""
        with open_store(ingested / "edgar.sqlite") as conn:
            conn.execute(
                "UPDATE filing SET form_type = '10-K' WHERE accession_no = '0000320193-99-999999'"
            )
            conn.commit()
        main(["extract", "--cik", "320193", "--json"], client=edgar_with_filings)
        payload = json.loads(capsys.readouterr().out)
        statuses = {r["accession"]: r["status"] for r in payload["results"]}
        assert statuses["0000320193-99-999999"] == "no_document"
        assert payload["failures"] == 0

    def test_a_filing_with_no_document_url_never_enters_the_resume_queue(
        self, ingested, edgar_with_filings, capsys
    ):
        with open_store(ingested / "edgar.sqlite") as conn:
            conn.execute(
                "UPDATE filing SET form_type = '10-K' WHERE accession_no = '0000320193-99-999999'"
            )
            conn.commit()
        main(["extract", "--cik", "320193", "--json"], client=edgar_with_filings)
        capsys.readouterr()
        main(["resume", "--json"], client=edgar_with_filings)
        assert json.loads(capsys.readouterr().out)["results"] == []


class TestMachineReadableOutput:
    """Every command speaks JSON on request.

    The CLI is the interface an agent drives, not just one a person types at. That only
    works if output is parseable and stdout carries nothing else — a stray progress line
    turns a parse into a guess.
    """

    def test_ingest_emits_only_json_on_stdout(self, data_dir, edgar, capsys):
        main(["ingest", "--cik", "320193", "--json"], client=edgar)
        payload = json.loads(capsys.readouterr().out)  # raises if prose crept in
        assert payload["command"] == "ingest"

    def test_ingest_reports_each_filer(self, data_dir, edgar, capsys):
        main(["ingest", "--cik", "320193", "--json"], client=edgar)
        results = json.loads(capsys.readouterr().out)["results"]
        assert len(results) == 1
        assert results[0]["cik"] == 320193
        assert results[0]["status"] == "ingested"
        assert results[0]["filings"] == 3
        assert results[0]["facts_inserted"] == 6

    def test_ingest_marks_a_cache_hit(self, data_dir, edgar, capsys):
        main(["ingest", "--cik", "320193"], client=edgar)
        capsys.readouterr()
        main(["ingest", "--cik", "320193", "--json"], client=edgar)
        assert json.loads(capsys.readouterr().out)["results"][0]["status"] == "cached"

    def test_ingest_reports_a_failure_without_breaking_the_parse(self, data_dir, capsys):
        from dossier.edgar import EdgarClient

        client = EdgarClient(
            VALID_UA,
            transport=httpx.MockTransport(lambda request: httpx.Response(500)),
            sleep=lambda _: None,
            max_retries=1,
        )
        assert main(["ingest", "--cik", "1", "--json"], client=client) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["failures"] == 1
        assert payload["results"][0]["status"] == "failed"
        assert payload["results"][0]["error"]

    def test_resume_emits_json(self, data_dir, capsys):
        assert main(["resume", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "resume"
        assert payload["results"] == []

    def test_resume_reports_what_it_retried(self, data_dir, edgar, capsys):
        from dossier.edgar import EdgarClient

        broken = EdgarClient(
            VALID_UA,
            transport=httpx.MockTransport(lambda request: httpx.Response(500)),
            sleep=lambda _: None,
            max_retries=1,
        )
        main(["ingest", "--cik", "320193"], client=broken)
        capsys.readouterr()

        main(["resume", "--json"], client=edgar)
        results = json.loads(capsys.readouterr().out)["results"]
        assert [r["status"] for r in results] == ["ingested"]

    def test_errors_go_to_stderr_so_stdout_stays_parseable(self, data_dir, monkeypatch, capsys):
        monkeypatch.delenv("EDGAR_USER_AGENT", raising=False)
        main(["ingest", "--cik", "320193", "--json"])
        captured = capsys.readouterr()
        assert captured.out.strip() == ""
        assert "EDGAR_USER_AGENT" in captured.err

    def test_human_output_is_unchanged_without_the_flag(self, data_dir, edgar, capsys):
        main(["ingest", "--cik", "320193"], client=edgar)
        out = capsys.readouterr().out
        assert "Ingesting" in out
        assert not out.lstrip().startswith("{")


class TestStatusCommand:
    def test_reports_an_empty_store(self, data_dir, capsys):
        assert main(["status"]) == 0
        out = capsys.readouterr().out
        assert "Filers:   0" in out
        assert "none 0" not in out  # reads like a status called "none"

    def test_counts_what_has_been_ingested(self, data_dir, edgar, capsys):
        main(["ingest", "--cik", "320193"], client=edgar)
        capsys.readouterr()
        main(["status"])
        out = capsys.readouterr().out
        assert "filers" in out.lower()
        assert "done" in out.lower()

    def test_json_output_is_machine_readable(self, data_dir, edgar, capsys):
        main(["ingest", "--cik", "320193"], client=edgar)
        capsys.readouterr()
        main(["status", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["filers"] == 1
        assert payload["jobs"]["done"] == 1


class TestResumeCommand:
    def test_retries_a_failed_job(self, data_dir, edgar, capsys):
        from dossier.edgar import EdgarClient

        broken = EdgarClient(
            VALID_UA,
            transport=httpx.MockTransport(lambda request: httpx.Response(500)),
            sleep=lambda _: None,
            max_retries=1,
        )
        main(["ingest", "--cik", "320193"], client=broken)
        capsys.readouterr()

        assert main(["resume"], client=edgar) == 0
        with open_store(data_dir / "edgar.sqlite") as conn:
            assert conn.execute("SELECT status FROM job").fetchone()["status"] == "done"
            assert conn.execute("SELECT COUNT(*) FROM filer").fetchone()[0] == 1

    def test_says_so_when_there_is_nothing_to_resume(self, data_dir, capsys):
        assert main(["resume"]) == 0
        assert "nothing" in capsys.readouterr().out.lower()
