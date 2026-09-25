"""The `dossier` command line.

Each stage is independently runnable and restartable, and communicates with the others
only through the store. A session cut off mid-run loses one filing's worth of work; the
next session's first command picks up exactly where it stopped.

Every command takes `--json`. This is the interface an agent drives as much as one a
person types at, and that only works if stdout carries parseable output and nothing
else — a stray progress line turns a parse into a guess. Errors always go to stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

from dossier import __version__
from dossier.analysis import (
    load_findings,
    prepare_pass_a,
    prepare_pass_b,
    prepare_pass_d,
    prompt_version_for,
)
from dossier.asof import AsOfView, fact_count
from dossier.bulk import COMPANYFACTS_URL, companyfacts_version, read_company_facts
from dossier.config import Config
from dossier.deregistrations import mark_terminal_status, parse_form_index, unknown_ciks
from dossier.edgar import EdgarClient, InvalidUserAgent, SecBlocked
from dossier.extract import EXTRACT_JOB, EXTRACTOR_VERSION, extract_filing, filings_to_extract
from dossier.formindex import read_quarters
from dossier.ingest import (
    BULK_FACTS_JOB,
    INGEST_JOB,
    INGEST_VERSION,
    held_ciks,
    ingest_facts,
    ingest_filer,
)
from dossier.jobs import JobQueue, Outcome
from dossier.prices import (
    NOT_INGESTED,
    PRICES_JOB,
    NoPriceData,
    YahooPrices,
    priceable_ciks,
    store_prices,
    ticker_of,
)
from dossier.recheck import recheck_all, recheck_thesis
from dossier.screens import (
    CANDIDATE_LIMIT,
    SCREEN_JOB,
    SCREENER_VERSION,
    build_candidates,
    store_candidates,
)
from dossier.store import open_store, row_counts
from dossier.thesis import (
    latest_as_of,
    load_bear_pass,
    load_thesis,
    prepare_bear_pass,
    prepare_thesis,
    record_pass_over,
)
from dossier.universe import parse_annual_filers, unheld
from dossier.valuation import load_valuation, prepare_valuation

#: How far back `dossier prices` fetches by default: enough for the screens to run as of
#: today, a year ago and two years ago.
DEFAULT_PRICE_HISTORY_DAYS = 3 * 366


def build_parser() -> argparse.ArgumentParser:
    # Options every subcommand shares. `--json` belongs on all of them, so it lives
    # here rather than being repeated and eventually forgotten on a new one.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit machine-readable JSON on stdout and nothing else",
    )

    parser = argparse.ArgumentParser(
        prog="dossier",
        description=(
            "Read SEC filings and produce cited value-investing dossiers. "
            "A research tool, not investment advice."
        ),
    )
    parser.add_argument("--version", action="version", version=f"dossier {__version__}")
    subcommands = parser.add_subparsers(dest="command", metavar="<command>")

    ingest = subcommands.add_parser(
        "ingest", parents=[common], help="fetch filers from EDGAR into the store"
    )
    ingest.add_argument(
        "--cik",
        action="append",
        type=int,
        default=[],
        metavar="CIK",
        help="a filer to ingest; repeatable",
    )
    ingest.add_argument(
        "--limit",
        type=int,
        help="ingest this many filers from the ticker map instead of naming them",
    )
    ingest.add_argument(
        "--force", action="store_true", help="re-run even for filers already ingested"
    )
    ingest.add_argument(
        "--bulk",
        action="store_true",
        help="read facts for filers already in the store from EDGAR's nightly "
        "companyfacts.zip, downloaded once and kept on disk; --cik narrows it",
    )
    ingest.add_argument(
        "--refresh",
        action="store_true",
        help="with --bulk, download a fresh companyfacts.zip even if one is on disk",
    )

    extract = subcommands.add_parser(
        "extract",
        parents=[common],
        help="pull Item sections out of filings already in the store",
    )
    extract.add_argument("--cik", type=int, metavar="CIK", help="extract this filer's 10-Ks")
    extract.add_argument(
        "--form",
        default="10-K",
        choices=["10-K", "DEF 14A"],
        help="which form to extract (the proxy carries the compensation sections)",
    )
    extract.add_argument("--accession", metavar="ACCESSION", help="extract one named filing")
    extract.add_argument("--limit", type=int, help="stop after this many filings")
    extract.add_argument("--force", action="store_true", help="re-extract filings already done")

    prices = subcommands.add_parser(
        "prices",
        parents=[common],
        help="fetch daily closes for ingested filers, split adjustment undone",
    )
    prices.add_argument(
        "--cik",
        action="append",
        type=int,
        default=[],
        metavar="CIK",
        help="a filer to price; repeatable",
    )
    prices.add_argument(
        "--all", action="store_true", help="price every ingested filer that has a ticker"
    )
    prices.add_argument(
        "--since",
        type=date.fromisoformat,
        help="earliest date to fetch (default: three years ago)",
    )

    screen = subcommands.add_parser(
        "screen",
        parents=[common],
        help="run the five screens as of a date and list the candidates",
    )
    screen.add_argument(
        "--as-of",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="screen with only what was knowable on this date (default: today)",
    )
    screen.add_argument(
        "--limit",
        type=int,
        default=CANDIDATE_LIMIT,
        help=f"most candidates to list (default: {CANDIDATE_LIMIT})",
    )
    screen.add_argument(
        "--out",
        metavar="FILE",
        help="write the candidate array here: the analysis layer's only input",
    )
    screen.add_argument(
        "--compare",
        metavar="MONTHS",
        help="also screen this many months earlier and record what changed, "
        "e.g. --compare 12,24. A company cheap for two years is a different animal "
        "from one that fell in this quarter",
    )

    analyze = subcommands.add_parser(
        "analyze",
        parents=[common],
        help="prepare an analysis pass, or load its findings back",
    )
    analyze.add_argument("--pass", dest="pass_name", default="a", choices=["a", "b", "d"])
    analyze.add_argument("--cik", type=int, required=True, metavar="CIK")
    analyze.add_argument(
        "--item",
        help="the section to read (default: 1A for pass a, 8 for pass b)",
    )
    analyze.add_argument(
        "--prepare",
        action="store_true",
        help="write the pass's input for a model to read",
    )
    analyze.add_argument(
        "--load",
        metavar="FILE",
        help="read findings back, validate every quote, and store what survives",
    )
    analyze.add_argument("--out", metavar="FILE", help="write prepared input here")

    valuation = subcommands.add_parser(
        "value",
        parents=[common],
        help="prepare a valuation's input, or load its assumptions back",
    )
    valuation.add_argument("--cik", type=int, required=True, metavar="CIK")
    valuation.add_argument(
        "--as-of", dest="as_of", metavar="YYYY-MM-DD", help="value as of this date"
    )
    valuation.add_argument(
        "--prepare",
        action="store_true",
        help="write the figures, the findings and the fixed rules for a model to read",
    )
    valuation.add_argument(
        "--load",
        metavar="FILE",
        help="read assumptions back, validate every justification, and store the range",
    )
    valuation.add_argument("--out", metavar="FILE", help="write prepared input here")

    thesis = subcommands.add_parser(
        "thesis",
        parents=[common],
        help="write or attack a thesis, and record it in the journal",
    )
    thesis.add_argument("--cik", type=int, required=True, metavar="CIK")
    thesis.add_argument(
        "--as-of", dest="as_of", metavar="YYYY-MM-DD", help="the valuation to argue from"
    )
    thesis.add_argument(
        "--bear",
        action="store_true",
        help="the bear pass: attack the stored thesis rather than writing one",
    )
    thesis.add_argument(
        "--prepare", action="store_true", help="write the pass's input for a model to read"
    )
    thesis.add_argument(
        "--load",
        metavar="FILE",
        help="read it back, validate it, store it and write the journal entry",
    )
    thesis.add_argument(
        "--pass-over",
        dest="pass_over",
        metavar="REASON",
        help="record a candidate that cleared screening and was passed over, and why",
    )
    thesis.add_argument("--out", metavar="FILE", help="write prepared input here")

    recheck = subcommands.add_parser(
        "recheck",
        parents=[common],
        help="re-check every open thesis against its own falsification conditions",
    )
    recheck.add_argument(
        "--cik", type=int, metavar="CIK", help="one company instead of every open thesis"
    )
    recheck.add_argument(
        "--as-of", dest="as_of", metavar="YYYY-MM-DD", help="which thesis, by its date"
    )
    recheck.add_argument(
        "--on",
        metavar="YYYY-MM-DD",
        help="re-check as it would have read on this date (default: today)",
    )

    deregistrations = subcommands.add_parser(
        "deregistrations",
        parents=[common],
        help="find filers that stopped filing and record why, from EDGAR's form index",
    )
    deregistrations.add_argument(
        "--from-year", dest="from_year", type=int, required=True, metavar="YYYY"
    )
    deregistrations.add_argument(
        "--to-year",
        dest="to_year",
        type=int,
        metavar="YYYY",
        help="inclusive; defaults to --from-year",
    )
    deregistrations.add_argument(
        "--ingest",
        action="store_true",
        help="also ingest the dead filers the store has never heard of — the "
        "survivorship gap itself, since the ticker map can never find them",
    )

    universe = subcommands.add_parser(
        "universe",
        parents=[common],
        help="find every filer that produced an annual report, from EDGAR's form index — "
        "including the ones that no longer trade",
    )
    universe.add_argument("--from-year", dest="from_year", type=int, required=True, metavar="YYYY")
    universe.add_argument(
        "--to-year",
        dest="to_year",
        type=int,
        metavar="YYYY",
        help="inclusive; defaults to --from-year",
    )
    universe.add_argument(
        "--ingest",
        action="store_true",
        help="also ingest the filers the store does not hold: two requests each",
    )
    universe.add_argument("--limit", type=int, help="with --ingest, fetch at most this many filers")

    subcommands.add_parser(
        "status", parents=[common], help="what is in the store and what work is pending"
    )
    subcommands.add_parser(
        "resume", parents=[common], help="retry jobs that are pending or have failed"
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    client: EdgarClient | None = None,
    prices: YahooPrices | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        # Show the whole menu rather than a bare usage line: someone who typed
        # `dossier` is asking what it does.
        parser.print_help(sys.stderr)
        return 2

    config = Config.load()
    try:
        if args.command == "ingest":
            return _ingest(args, config, client)
        if args.command == "extract":
            return _extract(args, config, client)
        if args.command == "prices":
            return _prices(args, config, prices)
        if args.command == "screen":
            return _screen(args, config)
        if args.command == "analyze":
            return _analyze(args, config)
        if args.command == "value":
            return _value(args, config)
        if args.command == "thesis":
            return _thesis(args, config)
        if args.command == "recheck":
            return _recheck(args, config)
        if args.command == "deregistrations":
            return _deregistrations(args, config, client)
        if args.command == "universe":
            return _universe(args, config, client)
        if args.command == "status":
            return _status(args, config)
        if args.command == "resume":
            return _resume(args, config, client, prices)
    except InvalidUserAgent as exc:
        # The most likely first-run failure in the whole tool. It should read as an
        # instruction, not as a stack trace — and never on stdout, which may be a pipe.
        print(str(exc), file=sys.stderr)
        return 2
    except SecBlocked as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


def _emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _say(as_json: bool, message: str) -> None:
    """Human-facing progress. Suppressed entirely under --json."""
    if not as_json:
        print(message)


def _client(client: EdgarClient | None) -> EdgarClient:
    return client if client is not None else EdgarClient.from_env()


def _result(outcome: Outcome, **identity) -> dict:
    """The part of every per-unit result that `JobQueue.run` already decided."""
    return {**identity, "status": outcome.status, "error": outcome.error}


def _record_fields(value) -> str:
    """Store a result dataclass as its fields."""
    return json.dumps(value.__dict__, default=list)


def _ingest_one(queue: JobQueue, conn, edgar: EdgarClient, cik: int, force: bool) -> dict:
    """Ingest one filer as one job. Returns a structured result for either output mode."""

    def work():
        return ingest_filer(conn, edgar.submissions(cik), edgar.company_facts(cik))

    outcome = queue.run(
        INGEST_JOB,
        {"cik": cik},
        work,
        prompt_version=INGEST_VERSION,
        record=_record_fields,
        force=force,
    )
    result = _result(outcome, cik=cik)
    if outcome.status == "done":
        ingested = outcome.value
        result.update(
            status="ingested",
            filings=ingested.filings,
            facts=ingested.facts,
            facts_inserted=ingested.facts_inserted,
            stub_filings=ingested.stub_filings,
        )
    return result


def _describe(result: dict) -> str:
    if result["status"] == "cached":
        return f"  {result['cik']}: cached"
    if result["status"] == "ingested":
        return (
            f"  {result['cik']}: {result['filings']} filings, {result['facts_inserted']} new facts"
        )
    return f"  {result['cik']}: {result['status']} — {result['error']}"


def _ingest_facts_one(queue: JobQueue, conn, zf: zipfile.ZipFile, version: str, cik: int) -> dict:
    """One filer's facts from the bulk ZIP, as one job keyed on the ZIP's version."""

    def work():
        doc = read_company_facts(zf, cik)
        return doc is not None, ingest_facts(conn, cik, doc)

    outcome = queue.run(
        BULK_FACTS_JOB,
        {"cik": cik, "companyfacts": version},
        work,
        prompt_version=INGEST_VERSION,
        record=lambda value: _record_fields(value[1]),
    )
    result = _result(outcome, cik=cik)
    if outcome.status == "done":
        found, ingested = outcome.value
        # A filer with no entry is one with no XBRL: an answer, like the API's 404.
        result.update(
            status="ingested" if found else "no_facts",
            facts=ingested.facts,
            facts_inserted=ingested.facts_inserted,
            stub_filings=ingested.stub_filings,
        )
    return result


def _ingest_bulk(args, config: Config, client: EdgarClient | None) -> int:
    """Facts for filers already in the store, from the nightly companyfacts.zip.

    The ZIP is downloaded only when it is missing or --refresh asks for a new one, so
    widening the tag set afterwards costs a local re-parse and no requests at all.
    """
    path = config.bulk_dir / "companyfacts.zip"
    if args.refresh or not path.exists():
        _say(args.as_json, f"Downloading {COMPANYFACTS_URL}")
        _client(client).download(COMPANYFACTS_URL, path)
    version = companyfacts_version(path)

    results = []
    with open_store(config.store_path) as conn, zipfile.ZipFile(path) as zf:
        ciks = list(args.cik) or held_ciks(conn)
        _say(args.as_json, f"Reading facts for {len(ciks)} filer(s) from the {version} ZIP")
        queue = JobQueue(conn, output_dir=config.output_dir)
        for cik in ciks:
            result = _ingest_facts_one(queue, conn, zf, version, cik)
            results.append(result)
            _say(args.as_json, f"  {cik}: {result['status']}")

    failures = sum(1 for r in results if r["status"] in ("failed", "skipped"))
    if args.as_json:
        _emit(
            {
                "command": "ingest",
                "bulk": str(path),
                "companyfacts": version,
                "requested": len(results),
                "failures": failures,
                "results": results,
            }
        )
    return 1 if failures else 0


def _ingest(args, config: Config, client: EdgarClient | None) -> int:
    if args.bulk:
        return _ingest_bulk(args, config, client)
    edgar = _client(client)
    ciks = list(args.cik)
    if args.limit is not None:
        # The universe, cheapest step first: the ticker map is one small request.
        ciks = sorted(edgar.ticker_map())[: args.limit]
    if not ciks:
        print("dossier ingest: give it --cik or --limit", file=sys.stderr)
        return 2

    _say(args.as_json, f"Ingesting {len(ciks)} filer(s) into {config.store_path}")
    results = []
    with open_store(config.store_path) as conn:
        queue = JobQueue(conn, output_dir=config.output_dir)
        for cik in ciks:
            result = _ingest_one(queue, conn, edgar, cik, args.force)
            results.append(result)
            _say(args.as_json, _describe(result))

    failures = sum(1 for r in results if r["status"] in ("failed", "skipped"))
    if args.as_json:
        _emit(
            {
                "command": "ingest",
                "store": str(config.store_path),
                "requested": len(ciks),
                "failures": failures,
                "results": results,
            }
        )
    elif failures:
        print(f"{failures} filer(s) failed; `dossier resume` will retry them", file=sys.stderr)
    return 1 if failures else 0


def _extract_one(queue: JobQueue, conn, edgar: EdgarClient, filing, force: bool) -> dict:
    """Extract one filing as one job. Filings are immutable, so the cache never staless."""
    accession = filing["accession_no"]
    if not filing["primary_doc_url"]:
        # A stub filing, created during ingest for an accession outside the submissions
        # window. There is no document to fetch and nothing to retry until ingest
        # supplies a URL, so this is reported and skipped rather than failed — a failed
        # job would sit in the resume queue forever, retrying what cannot succeed.
        return {
            "accession": accession,
            "status": "no_document",
            "error": "filing has no primary_doc_url",
        }

    def work():
        html = edgar.get(filing["primary_doc_url"]).text
        return extract_filing(conn, accession, html, form_type=filing["form_type"])

    outcome = queue.run(
        EXTRACT_JOB,
        {"accession": accession},
        work,
        prompt_version=EXTRACTOR_VERSION,
        record=_record_fields,
        force=force,
    )
    result = _result(outcome, accession=accession)
    if outcome.status == "done":
        extracted = outcome.value
        result.update(
            status="extracted",
            sections=extracted.sections,
            items=extracted.items,
            lowest_confidence=extracted.lowest_confidence,
        )
    return result


def _describe_extract(result: dict) -> str:
    if result["status"] == "cached":
        return f"  {result['accession']}: cached"
    if result["status"] == "extracted":
        items = ", ".join(result["items"])
        return (
            f"  {result['accession']}: {result['sections']} sections ({items}), "
            f"weakest {result['lowest_confidence']:.2f}"
        )
    return f"  {result['accession']}: {result['status']} — {result['error']}"


def _extract(args, config: Config, client: EdgarClient | None) -> int:
    if args.cik is None and args.accession is None:
        print("dossier extract: give it --cik or --accession", file=sys.stderr)
        return 2

    results = []
    with open_store(config.store_path) as conn:
        filings = filings_to_extract(conn, cik=args.cik, form=args.form, accession=args.accession)
        if args.limit is not None:
            filings = filings[: args.limit]

        if filings:
            edgar = _client(client)
            _say(args.as_json, f"Extracting {len(filings)} filing(s)")
            queue = JobQueue(conn, output_dir=config.output_dir)
            for filing in filings:
                result = _extract_one(queue, conn, edgar, filing, args.force)
                results.append(result)
                _say(args.as_json, _describe_extract(result))
        else:
            _say(args.as_json, "No filings to extract. Ingest some first.")

    failures = sum(1 for r in results if r["status"] in ("failed", "skipped"))
    if args.as_json:
        _emit(
            {
                "command": "extract",
                "extracted": len(results),
                "failures": failures,
                "results": results,
            }
        )
    return 1 if failures else 0


def _prices_one(
    queue: JobQueue, conn, source: YahooPrices, cik: int, since: date, through: date
) -> dict:
    """Price one filer as one job.

    Unlike a filing, a price history grows every trading day, so the job is keyed by the
    day it runs through: re-running the same day is a cache hit, the next day is not.
    """
    result: dict = {"cik": cik, "ticker": None, "status": None, "error": None}
    ticker = ticker_of(conn, cik)
    if ticker is NOT_INGESTED:
        result["status"] = "not_ingested"
        result["error"] = "run `dossier ingest` for this filer first"
        return result
    result["ticker"] = ticker
    if not ticker:
        # Nothing to retry until ingest supplies a ticker, so this is not a failure.
        result["status"] = "no_ticker"
        return result

    def work():
        try:
            points = source.daily(ticker, since, through)
        except NoPriceData as exc:
            # An answer, not a failure: the source has nothing for this ticker, and a
            # failed job would sit in the resume queue retrying what cannot succeed.
            # The key carries the date, so tomorrow's run asks again.
            return {"days": 0, "inserted": 0, "no_data": f"{type(exc).__name__}: {exc}"}
        inserted = store_prices(conn, cik, ticker, points)
        latest = max((p.price_date for p in points), default=None)
        return {"days": len(points), "inserted": inserted, "latest": latest}

    inputs = {
        "cik": cik,
        "ticker": ticker,
        "since": since.isoformat(),
        "through": through.isoformat(),
    }
    outcome = queue.run(PRICES_JOB, inputs, work)
    result.update(status=outcome.status, error=outcome.error)
    if outcome.status == "done":
        fetched = outcome.value
        if "no_data" in fetched:
            result.update(status="no_data", error=fetched["no_data"])
        else:
            result.update(
                status="fetched",
                days=fetched["days"],
                days_inserted=fetched["inserted"],
                latest=fetched["latest"],
            )
    return result


def _prices(args, config: Config, source: YahooPrices | None) -> int:
    through = date.today()
    since = args.since or through - timedelta(days=DEFAULT_PRICE_HISTORY_DAYS)
    results = []
    with open_store(config.store_path) as conn:
        ciks = list(args.cik)
        if args.all:
            ciks += priceable_ciks(conn)
        if not ciks:
            print("dossier prices: give it --cik or --all", file=sys.stderr)
            return 2

        owned = source is None
        source = source or YahooPrices()
        try:
            queue = JobQueue(conn, output_dir=config.output_dir)
            _say(args.as_json, f"Pricing {len(ciks)} filer(s) since {since}")
            for cik in dict.fromkeys(ciks):
                result = _prices_one(queue, conn, source, cik, since, through)
                results.append(result)
                _say(args.as_json, f"  {cik}: {result['status']}")
        finally:
            if owned:
                source.close()

    failures = sum(1 for r in results if r["status"] in ("failed", "skipped", "not_ingested"))
    if args.as_json:
        _emit(
            {
                "command": "prices",
                "since": since.isoformat(),
                "through": through.isoformat(),
                "requested": len(results),
                "failures": failures,
                "results": results,
            }
        )
    return 1 if failures else 0


def _screen(args, config: Config) -> int:
    """Screen as of a date, as one job.

    The job is keyed on the date *and* on how much of the store was visible by then:
    ingesting another filer changes the answer for a date already screened.
    """
    as_of = args.as_of or date.today()
    try:
        compare = tuple(int(part) for part in args.compare.split(",")) if args.compare else ()
    except ValueError:
        print("dossier screen: --compare takes months, e.g. --compare 12,24", file=sys.stderr)
        return 2
    with open_store(config.store_path) as conn:
        queue = JobQueue(conn, output_dir=config.output_dir)
        inputs = {
            "as_of": as_of.isoformat(),
            "limit": args.limit,
            "compare": list(compare),
            "fingerprint": AsOfView(conn, as_of).fingerprint(),
        }

        def work():
            built = build_candidates(conn, as_of, limit=args.limit, compare_months=compare)
            store_candidates(conn, built)
            return built

        outcome = queue.run(SCREEN_JOB, inputs, work, prompt_version=SCREENER_VERSION)
        if outcome.status in ("failed", "skipped"):
            print(f"dossier screen: {outcome.error}", file=sys.stderr)
            return 1
        if outcome.status == "cached":
            run = json.loads(outcome.job.read_output())
            store_candidates(conn, run)
            status = "cached"
        else:
            run, status = outcome.value, "screened"

    if args.out:
        Path(args.out).write_text(
            json.dumps(run["candidates"], indent=2, default=str), encoding="utf-8"
        )
    if args.as_json:
        _emit({"command": "screen", "status": status, **run})
        return 0

    universe = run["universe"]
    print(
        f"Screened as of {run['as_of']} ({status}): "
        f"{universe['eligible']} of {universe['filers']} filers eligible"
    )
    for reason, count in sorted(universe["excluded"].items(), key=lambda item: -item[1]):
        print(f"  excluded, {reason}: {count}")
    for screen, counts in run["screens"].items():
        print(f"  {screen}: {counts['flagged']} flagged of {counts['ranked']} ranked")
    if run["candidates"]:
        print("Candidates:")
    for candidate in run["candidates"]:
        trend = f"[{candidate['trend']}] " if candidate.get("trend") else ""
        print(f"  {trend}{candidate['ticker'] or candidate['cik']}: {candidate['flag_reason']}")
    return 0


def _deregistrations(args, config: Config, client: EdgarClient | None) -> int:
    """Record which filers stopped filing, from the quarterly form index.

    One file per quarter covers every filer in it, so this is four requests a year rather
    than a crawl — and it is the only way to find the companies that failed, because a
    filer that no longer trades is not in the ticker map to be looked up.
    """
    to_year = args.to_year or args.from_year
    if to_year < args.from_year:
        print("dossier deregistrations: --to-year is before --from-year", file=sys.stderr)
        return 2

    edgar = _client(client)
    found, quarters = read_quarters(
        edgar.form_index,
        args.from_year,
        to_year,
        parse=parse_form_index,
        label="terminal_filings",
    )

    with open_store(config.store_path) as conn:
        marked = mark_terminal_status(conn, found)
        unknown = unknown_ciks(conn, found)
        ingested = []
        if args.ingest and unknown:
            _say(args.as_json, f"Ingesting {len(unknown)} filer(s) the store did not hold")
            queue = JobQueue(conn, output_dir=config.output_dir)
            for record in unknown:
                ingested.append(_ingest_one(queue, conn, edgar, record.cik, force=False))
            mark_terminal_status(conn, found)

    result = {
        "command": "deregistrations",
        "years": [args.from_year, to_year],
        "quarters": quarters,
        "terminal_filings": len(found),
        "filers_marked": marked,
        "unknown_ciks": len(unknown),
        "ingested": ingested,
    }
    if args.as_json:
        _emit(result)
    else:
        print(f"Read {len(quarters)} quarter(s): {len(found)} terminal filings")
        print(f"Marked {marked} filer(s) the store holds")
        print(f"{len(unknown)} dead CIK(s) the store has never heard of", end="")
        if args.ingest:
            print(f", ingested {len(ingested)}")
        else:
            print(" — pass --ingest to fetch them")
    return 0


def _universe(args, config: Config, client: EdgarClient | None) -> int:
    """Find every filer that produced an annual report, and optionally fetch them.

    The ticker map lists who trades today; the form index lists who filed, dead or
    alive. Reading it is four requests a year. Fetching what it finds is two requests
    per filer, so that part waits for `--ingest`.
    """
    to_year = args.to_year or args.from_year
    if to_year < args.from_year:
        print("dossier universe: --to-year is before --from-year", file=sys.stderr)
        return 2

    edgar = _client(client)
    found, quarters = read_quarters(
        edgar.form_index,
        args.from_year,
        to_year,
        parse=parse_annual_filers,
        label="annual_filers",
    )

    with open_store(config.store_path) as conn:
        missing = unheld(conn, found)
        distinct = len({filer.cik for filer in found})
        ingested = []
        if args.ingest:
            to_fetch = missing if args.limit is None else missing[: args.limit]
            _say(args.as_json, f"Ingesting {len(to_fetch)} filer(s) the store did not hold")
            queue = JobQueue(conn, output_dir=config.output_dir)
            for filer in to_fetch:
                result = _ingest_one(queue, conn, edgar, filer.cik, force=False)
                ingested.append(result)
                _say(args.as_json, _describe(result))

    failures = sum(1 for r in ingested if r["status"] in ("failed", "skipped"))
    result = {
        "command": "universe",
        "years": [args.from_year, to_year],
        "quarters": quarters,
        "annual_filers": distinct,
        "held": distinct - len(missing),
        "not_held": len(missing),
        "ingested": ingested,
        "failures": failures,
    }
    if args.as_json:
        _emit(result)
    else:
        print(f"Read {len(quarters)} quarter(s): {distinct} filer(s) with an annual report")
        print(f"The store holds {result['held']}; {len(missing)} it has never heard of", end="")
        if args.ingest:
            print(f", ingested {len(ingested)}")
        else:
            print(" — pass --ingest to fetch them")
    return 1 if failures else 0


def _recheck(args, config: Config) -> int:
    """The quarterly re-check. It reports; it never advises.

    Exit 1 when anything was breached or could not be checked, so a scheduled run that
    nobody reads still says something a machine can act on.
    """
    with open_store(config.store_path) as conn:
        try:
            if args.cik:
                as_of = args.as_of or latest_as_of(conn, args.cik)
                if as_of is None:
                    print(f"dossier recheck: no thesis for CIK {args.cik}", file=sys.stderr)
                    return 2
                reports = [
                    recheck_thesis(
                        conn,
                        cik=args.cik,
                        as_of=as_of,
                        on=args.on,
                        journal_dir=config.journal_dir,
                    )
                ]
            else:
                reports = recheck_all(conn, on=args.on, journal_dir=config.journal_dir)
        except ValueError as exc:
            print(f"dossier recheck: {exc}", file=sys.stderr)
            return 2

    if args.as_json:
        _emit({"command": "recheck", "reports": reports})
    elif not reports:
        print("No theses to re-check.")
    else:
        for report in reports:
            print(
                f"CIK {report['cik']}, thesis v{report['thesis_version']} of "
                f"{report['as_of']}: {report['status']}"
            )
            for condition in report["conditions"]:
                said = condition["you_said"]
                observed = (
                    ", ".join(
                        f"{point['fy_end']}: "
                        f"{'n/a' if point['value'] is None else format(point['value'], '.4g')}"
                        for point in condition["observed"]
                    )
                    or "nothing measured"
                )
                print(
                    f"  [{condition['status']}] {said['metric']} {said['direction']} "
                    f"{said['threshold']} over {said['window']}"
                )
                print(f"      {observed}")
                if condition.get("note"):
                    print(f"      {condition['note']}")
            print(f"  {report['question']}")

    breached = sum(r["breached"] + r["needs_a_human"] for r in reports)
    return 1 if breached else 0


def _thesis(args, config: Config) -> int:
    """The thesis, the bear pass that attacks it, and the journal entry for each.

    The journal is the one artifact here that cannot be rebuilt from EDGAR, so it is
    written to the user's own directory — never inside this repository — and each entry
    gets its own file rather than editing one that already exists.
    """
    as_of = args.as_of or date.today().isoformat()
    journal_dir = config.journal_dir

    if args.pass_over:
        try:
            path = record_pass_over(journal_dir, cik=args.cik, as_of=as_of, reason=args.pass_over)
        except ValueError as exc:
            print(f"dossier thesis: {exc}", file=sys.stderr)
            return 2
        result = {"command": "thesis", "kind": "pass_over", "cik": args.cik, "journal_entry": path}
        if args.as_json:
            _emit(result)
        else:
            print(f"Recorded a pass-over for CIK {args.cik} in {path}")
        return 0

    if not args.prepare and not args.load:
        print(
            "dossier thesis: give it --prepare, --load FILE or --pass-over REASON", file=sys.stderr
        )
        return 2

    with open_store(config.store_path) as conn:
        prepare = prepare_bear_pass if args.bear else prepare_thesis
        if args.prepare:
            try:
                payload = prepare(conn, cik=args.cik, as_of=as_of)
            except ValueError as exc:
                print(f"dossier thesis: {exc}", file=sys.stderr)
                return 2
            if args.out:
                Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
                _say(
                    args.as_json,
                    f"Wrote {'bear pass' if args.bear else 'thesis'} input to {args.out}",
                )
            if args.as_json or not args.out:
                _emit(payload)
            return 0

        source = Path(args.load)
        if not source.exists():
            print(f"dossier thesis: no such file {source}", file=sys.stderr)
            return 2
        try:
            proposed = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"dossier thesis: {source} is not valid JSON — {exc}", file=sys.stderr)
            return 2

        load = load_bear_pass if args.bear else load_thesis
        try:
            result = load(
                conn, cik=args.cik, as_of=as_of, payload=proposed, journal_dir=journal_dir
            )
        except ValueError as exc:
            print(f"dossier thesis: {exc}", file=sys.stderr)
            return 2

    if args.as_json:
        _emit(result)
    elif args.bear:
        print(
            f"Bear pass on thesis v{result['thesis_version']}: kept {result['kept']}, "
            f"dropped {result['dropped']}"
        )
        for reason, count in sorted(result["drop_reasons"].items()):
            print(f"  {reason}: {count}")
        rate = result["fabrication_rate"]
        print(f"Fabrication rate: {'n/a' if rate is None else f'{rate:.0%}'}")
    else:
        print(f"Stored thesis v{result['version']} for CIK {result['cik']}")
        if result["journal_entry"]:
            print(f"Journal entry: {result['journal_entry']}")
    return 0 if not args.bear or result["dropped"] == 0 else 1


def _value(args, config: Config) -> int:
    """The same two halves as an analysis pass, for the same reason.

    The arithmetic runs here; the assumptions come from a model, as triples with a
    justification each. Nothing is stored unless every one of them passes.
    """
    if not args.prepare and not args.load:
        print("dossier value: give it --prepare or --load FILE", file=sys.stderr)
        return 2

    as_of = args.as_of or date.today().isoformat()
    with open_store(config.store_path) as conn:
        if args.prepare:
            try:
                payload = prepare_valuation(conn, cik=args.cik, as_of=as_of)
            except ValueError as exc:
                print(f"dossier value: {exc}", file=sys.stderr)
                return 2
            if args.out:
                Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
                _say(args.as_json, f"Wrote valuation input to {args.out}")
            if args.as_json or not args.out:
                _emit(payload)
            return 0

        source = Path(args.load)
        if not source.exists():
            print(f"dossier value: no such file {source}", file=sys.stderr)
            return 2
        try:
            proposed = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"dossier value: {source} is not valid JSON — {exc}", file=sys.stderr)
            return 2

        try:
            result = load_valuation(conn, cik=args.cik, as_of=as_of, payload=proposed)
        except ValueError as exc:
            print(f"dossier value: {exc}", file=sys.stderr)
            return 2

    if args.as_json:
        _emit(result)
    else:
        price = result["inputs"]["price"]
        print(f"Valuation of CIK {args.cik} as of {result['as_of']}, per share:")
        for case in ("bear", "base", "bull"):
            print(f"  {case:5} ${result[case]['per_share']:,.2f}")
        print(
            f"Buy below ${result['buy_below']:,.2f} "
            f"({result['margin_of_safety']:.0%} below the bear case)"
        )
        if price is not None:
            print(f"Price today ${price:,.2f}")
            implied = result["implied"]["growth"]
            if implied is not None:
                print(f"Today's price implies revenue growth of {implied:.1%} a year")
    return 0


def _analyze(args, config: Config) -> int:
    """Two halves with a person in the middle: prepare, then load.

    The model is a Claude Code session rather than an API call, so nothing here talks to
    one. `--prepare` hands over what the pass reads; `--load` takes the findings back
    and puts them through the validator.
    """
    if not args.prepare and not args.load:
        print("dossier analyze: give it --prepare or --load FILE", file=sys.stderr)
        return 2

    # Each pass reads its own section, so the default follows the pass rather than
    # making every caller remember that footnote forensics means Item 8.
    item = args.item or {"b": "8", "d": "CDA"}.get(args.pass_name, "1A")

    with open_store(config.store_path) as conn:
        if args.prepare:
            try:
                if args.pass_name == "b":
                    payload = prepare_pass_b(conn, cik=args.cik, item=item)
                elif args.pass_name == "d":
                    payload = prepare_pass_d(conn, cik=args.cik, item=item)
                else:
                    payload = prepare_pass_a(conn, cik=args.cik, item=item).to_dict()
            except ValueError as exc:
                print(f"dossier analyze: {exc}", file=sys.stderr)
                return 2
            if args.out:
                Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
                _say(args.as_json, f"Wrote Pass {args.pass_name} input to {args.out}")
            if args.as_json or not args.out:
                _emit(payload)
            return 0

        source = Path(args.load)
        if not source.exists():
            print(f"dossier analyze: no such file {source}", file=sys.stderr)
            return 2
        try:
            findings_payload = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"dossier analyze: {source} is not valid JSON — {exc}", file=sys.stderr)
            return 2

        try:
            result = load_findings(
                conn,
                cik=args.cik,
                payload=findings_payload,
                item=item,
                pass_name=args.pass_name,
            )
        except ValueError as exc:
            print(f"dossier analyze: {exc}", file=sys.stderr)
            return 2

    if args.as_json:
        _emit(
            {
                "command": "analyze",
                "pass": args.pass_name,
                "cik": args.cik,
                "prompt_version": prompt_version_for(item, pass_name=args.pass_name),
                "kept": result.kept,
                "dropped": result.dropped,
                "drop_reasons": result.drop_reasons,
                "fabrication_rate": result.fabrication_rate,
            }
        )
    else:
        print(f"Kept {result.kept} finding(s), dropped {result.dropped}")
        for reason, count in sorted(result.drop_reasons.items()):
            print(f"  {reason}: {count}")
        rate = result.fabrication_rate
        # None, not 0.0, when there was nothing to measure: a pass that produced no
        # findings has not earned a clean bill of health.
        print(f"Fabrication rate: {'n/a' if rate is None else f'{rate:.0%}'}")
    return 1 if result.dropped else 0


def _status(args, config: Config) -> int:
    with open_store(config.store_path) as conn:
        queue = JobQueue(conn, output_dir=config.output_dir)
        payload = {
            "command": "status",
            "store": str(config.store_path),
            **row_counts(conn),
            "facts": fact_count(conn),
            "jobs": queue.status_counts(),
            "resumable": len(queue.resumable()),
        }
    if args.as_json:
        _emit(payload)
        return 0
    print(f"Store:    {payload['store']}")
    print(f"Filers:   {payload['filers']}")
    print(f"Filings:  {payload['filings']}")
    print(f"Facts:    {payload['facts']}")
    summary = ", ".join(f"{status} {count}" for status, count in sorted(payload["jobs"].items()))
    print(f"Jobs:     {summary or 'none yet'}")
    if payload["resumable"]:
        print(f"\n{payload['resumable']} job(s) can be resumed with `dossier resume`")
    return 0


def _resume(
    args, config: Config, client: EdgarClient | None, source: YahooPrices | None = None
) -> int:
    results = []
    owned = None
    with open_store(config.store_path) as conn:
        queue = JobQueue(conn, output_dir=config.output_dir)
        queue.reclaim_stale()
        pending = queue.resumable()

        if pending:
            _say(args.as_json, f"Resuming {len(pending)} job(s)")
        else:
            _say(args.as_json, "Nothing to resume.")
        try:
            for job in pending:
                if job.job_type == INGEST_JOB:
                    client = _client(client)
                    result = _ingest_one(queue, conn, client, job.inputs["cik"], force=False)
                    _say(args.as_json, _describe(result))
                elif job.job_type == PRICES_JOB:
                    if source is None:
                        source = owned = YahooPrices()
                    # The job's own dates, so the retry claims this row rather than
                    # enqueueing a new one under today's key.
                    result = _prices_one(
                        queue,
                        conn,
                        source,
                        job.inputs["cik"],
                        date.fromisoformat(job.inputs["since"]),
                        date.fromisoformat(job.inputs["through"]),
                    )
                    _say(args.as_json, f"  {result['cik']}: {result['status']}")
                else:
                    result = {
                        "job_id": job.job_id,
                        "job_type": job.job_type,
                        "status": "unhandled",
                        "error": f"no handler for {job.job_type}",
                    }
                    _say(args.as_json, f"  {job.job_id}: no handler for {job.job_type}")
                results.append(result)
        finally:
            if owned is not None:
                owned.close()

    # A job left untouched is not a success. Counting it here is what stops a scheduled
    # resume that nobody reads from reporting that all is well.
    failures = sum(1 for r in results if r["status"] in ("failed", "skipped", "unhandled"))
    if args.as_json:
        _emit(
            {
                "command": "resume",
                "resumed": len(results),
                "failures": failures,
                "results": results,
            }
        )
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
