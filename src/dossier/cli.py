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
from pathlib import Path

from dossier import __version__
from dossier.analysis import PASS_A_VERSION, load_findings, prepare_pass_a
from dossier.asof import fact_count
from dossier.config import Config
from dossier.edgar import EdgarClient, InvalidUserAgent, SecBlocked
from dossier.extract import EXTRACTOR_VERSION, extract_filing
from dossier.ingest import ingest_filer
from dossier.jobs import JobQueue, idempotency_key
from dossier.store import open_store


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

    extract = subcommands.add_parser(
        "extract",
        parents=[common],
        help="pull Item sections out of filings already in the store",
    )
    extract.add_argument("--cik", type=int, metavar="CIK", help="extract this filer's 10-Ks")
    extract.add_argument("--accession", metavar="ACCESSION", help="extract one named filing")
    extract.add_argument("--limit", type=int, help="stop after this many filings")
    extract.add_argument("--force", action="store_true", help="re-extract filings already done")

    analyze = subcommands.add_parser(
        "analyze",
        parents=[common],
        help="prepare an analysis pass, or load its findings back",
    )
    analyze.add_argument("--pass", dest="pass_name", default="a", choices=["a"])
    analyze.add_argument("--cik", type=int, required=True, metavar="CIK")
    analyze.add_argument("--item", default="1A", help="the section to compare")
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

    subcommands.add_parser(
        "status", parents=[common], help="what is in the store and what work is pending"
    )
    subcommands.add_parser(
        "resume", parents=[common], help="retry jobs that are pending or have failed"
    )
    return parser


def main(argv: list[str] | None = None, *, client: EdgarClient | None = None) -> int:
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
        if args.command == "analyze":
            return _analyze(args, config)
        if args.command == "status":
            return _status(args, config)
        if args.command == "resume":
            return _resume(args, config, client)
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


def _ingest_one(queue: JobQueue, conn, edgar: EdgarClient, cik: int, force: bool) -> dict:
    """Ingest one filer as one job. Returns a structured result for either output mode."""
    inputs = {"cik": cik}
    key = idempotency_key("ingest_filer", inputs)
    result: dict = {"cik": cik, "status": None, "error": None}

    if queue.completed(key) is not None and not force:
        result["status"] = "cached"
        return result

    job = queue.enqueue("ingest_filer", inputs)
    if force and job.status == "done":
        queue.reopen(job)
    claimed = queue.claim_by_key(key)
    if claimed is None:
        result["status"] = "skipped"
        result["error"] = "too many failed attempts"
        return result

    try:
        submissions = edgar.submissions(cik)
        facts = edgar.company_facts(cik)
        ingested = ingest_filer(conn, submissions, facts)
    except Exception as exc:  # one bad filer must not cost the other 499
        queue.fail(claimed, f"{type(exc).__name__}: {exc}")
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    queue.finish(claimed, json.dumps(ingested.__dict__, default=list))
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


def _ingest(args, config: Config, client: EdgarClient | None) -> int:
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
    inputs = {"accession": accession}
    key = idempotency_key("extract_sections", inputs, prompt_version=EXTRACTOR_VERSION)
    result: dict = {"accession": accession, "status": None, "error": None}

    if not filing["primary_doc_url"]:
        # A stub filing, created during ingest for an accession outside the submissions
        # window. There is no document to fetch and nothing to retry until ingest
        # supplies a URL, so this is reported and skipped rather than failed — a failed
        # job would sit in the resume queue forever, retrying what cannot succeed.
        result["status"] = "no_document"
        result["error"] = "filing has no primary_doc_url"
        return result

    if queue.completed(key) is not None and not force:
        result["status"] = "cached"
        return result

    job = queue.enqueue("extract_sections", inputs, prompt_version=EXTRACTOR_VERSION)
    if force and job.status == "done":
        queue.reopen(job)
    claimed = queue.claim_by_key(key)
    if claimed is None:
        result["status"] = "skipped"
        result["error"] = "too many failed attempts"
        return result

    try:
        html = edgar.get(filing["primary_doc_url"]).text
        extracted = extract_filing(conn, accession, html)
    except Exception as exc:
        queue.fail(claimed, f"{type(exc).__name__}: {exc}")
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    queue.finish(claimed, json.dumps(extracted.__dict__, default=list))
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
        if args.accession is not None:
            filings = conn.execute(
                "SELECT * FROM filing WHERE accession_no = ?", (args.accession,)
            ).fetchall()
        else:
            # Pass A diffs consecutive 10-Ks, so that is what is worth extracting.
            filings = conn.execute(
                "SELECT * FROM filing WHERE cik = ? AND form_type = '10-K' "
                "ORDER BY filed_date DESC",
                (args.cik,),
            ).fetchall()
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


def _analyze(args, config: Config) -> int:
    """Two halves with a person in the middle: prepare, then load.

    The model is a Claude Code session rather than an API call, so nothing here talks to
    one. `--prepare` hands over what the pass reads; `--load` takes the findings back
    and puts them through the validator.
    """
    if not args.prepare and not args.load:
        print("dossier analyze: give it --prepare or --load FILE", file=sys.stderr)
        return 2

    with open_store(config.store_path) as conn:
        if args.prepare:
            try:
                prepared = prepare_pass_a(conn, cik=args.cik, item=args.item)
            except ValueError as exc:
                print(f"dossier analyze: {exc}", file=sys.stderr)
                return 2
            payload = prepared.to_dict()
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

        result = load_findings(
            conn,
            cik=args.cik,
            payload=findings_payload,
            item=args.item,
            pass_name=args.pass_name,
        )

    if args.as_json:
        _emit(
            {
                "command": "analyze",
                "pass": args.pass_name,
                "cik": args.cik,
                "prompt_version": PASS_A_VERSION,
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
            "filers": conn.execute("SELECT COUNT(*) FROM filer").fetchone()[0],
            "filings": conn.execute("SELECT COUNT(*) FROM filing").fetchone()[0],
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


def _resume(args, config: Config, client: EdgarClient | None) -> int:
    results = []
    with open_store(config.store_path) as conn:
        queue = JobQueue(conn, output_dir=config.output_dir)
        queue.reclaim_stale()
        pending = queue.resumable()

        if pending:
            edgar = _client(client)
            _say(args.as_json, f"Resuming {len(pending)} job(s)")
            for job in pending:
                if job.job_type != "ingest_filer":
                    results.append(
                        {
                            "job_id": job.job_id,
                            "job_type": job.job_type,
                            "status": "unhandled",
                            "error": f"no handler for {job.job_type}",
                        }
                    )
                    _say(args.as_json, f"  {job.job_id}: no handler for {job.job_type}, leaving it")
                    continue
                result = _ingest_one(queue, conn, edgar, job.inputs["cik"], force=False)
                results.append(result)
                _say(args.as_json, _describe(result))
        else:
            _say(args.as_json, "Nothing to resume.")

    failures = sum(1 for r in results if r["status"] in ("failed", "skipped"))
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
