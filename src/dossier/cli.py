"""The `dossier` command line.

Each stage is independently runnable and restartable, and communicates with the others
only through the store. A session cut off mid-run loses one filing's worth of work; the
next session's first command picks up exactly where it stopped.
"""

from __future__ import annotations

import argparse
import json
import sys

from dossier import __version__
from dossier.asof import fact_count
from dossier.config import Config
from dossier.edgar import EdgarClient, InvalidUserAgent, SecBlocked
from dossier.ingest import ingest_filer
from dossier.jobs import JobQueue, idempotency_key
from dossier.store import open_store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dossier",
        description=(
            "Read SEC filings and produce cited value-investing dossiers. "
            "A research tool, not investment advice."
        ),
    )
    parser.add_argument("--version", action="version", version=f"dossier {__version__}")
    subcommands = parser.add_subparsers(dest="command", metavar="<command>")

    ingest = subcommands.add_parser("ingest", help="fetch filers from EDGAR into the store")
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
        "--force",
        action="store_true",
        help="re-run even for filers already ingested",
    )

    status = subcommands.add_parser("status", help="what is in the store and what work is pending")
    status.add_argument("--json", action="store_true", help="machine-readable output")

    subcommands.add_parser("resume", help="retry jobs that are pending or have failed")
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
        if args.command == "status":
            return _status(args, config)
        if args.command == "resume":
            return _resume(config, client)
    except InvalidUserAgent as exc:
        # The most likely first-run failure in the whole tool. It should read as an
        # instruction, not as a stack trace.
        print(str(exc), file=sys.stderr)
        return 2
    except SecBlocked as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


def _client(client: EdgarClient | None) -> EdgarClient:
    return client if client is not None else EdgarClient.from_env()


def _ingest_one(queue: JobQueue, conn, edgar: EdgarClient, cik: int, force: bool) -> str:
    """Ingest one filer as one job. Returns a one-line report for the console."""
    inputs = {"cik": cik}
    key = idempotency_key("ingest_filer", inputs)
    cached = queue.completed(key)
    if cached is not None and not force:
        return f"  {cik}: cached"

    job = queue.enqueue("ingest_filer", inputs)
    if force and job.status == "done":
        queue.reopen(job)
    claimed = queue.claim_by_key(key)
    if claimed is None:
        return f"  {cik}: skipped (too many failed attempts)"

    try:
        submissions = edgar.submissions(cik)
        facts = edgar.company_facts(cik)
        result = ingest_filer(conn, submissions, facts)
    except Exception as exc:  # one bad filer must not cost the other 499
        queue.fail(claimed, f"{type(exc).__name__}: {exc}")
        return f"  {cik}: failed — {type(exc).__name__}: {exc}"

    queue.finish(claimed, json.dumps(result.__dict__, default=list))
    return f"  {cik}: {result.filings} filings, {result.facts_inserted} new facts"


def _ingest(args, config: Config, client: EdgarClient | None) -> int:
    edgar = _client(client)
    ciks = list(args.cik)
    if args.limit is not None:
        # The universe, cheapest step first: the ticker map is one small request.
        ciks = sorted(edgar.ticker_map())[: args.limit]
    if not ciks:
        print("dossier ingest: give it --cik or --limit", file=sys.stderr)
        return 2

    print(f"Ingesting {len(ciks)} filer(s) into {config.store_path}")
    failures = 0
    with open_store(config.store_path) as conn:
        queue = JobQueue(conn, output_dir=config.output_dir)
        for cik in ciks:
            line = _ingest_one(queue, conn, edgar, cik, args.force)
            if "failed" in line:
                failures += 1
            print(line)
    if failures:
        print(f"{failures} filer(s) failed; `dossier resume` will retry them", file=sys.stderr)
    return 1 if failures else 0


def _status(args, config: Config) -> int:
    with open_store(config.store_path) as conn:
        queue = JobQueue(conn, output_dir=config.output_dir)
        payload = {
            "store": str(config.store_path),
            "filers": conn.execute("SELECT COUNT(*) FROM filer").fetchone()[0],
            "filings": conn.execute("SELECT COUNT(*) FROM filing").fetchone()[0],
            "facts": fact_count(conn),
            "jobs": queue.status_counts(),
            "resumable": len(queue.resumable()),
        }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    print(f"Store:    {payload['store']}")
    print(f"Filers:   {payload['filers']}")
    print(f"Filings:  {payload['filings']}")
    print(f"Facts:    {payload['facts']}")
    jobs = payload["jobs"]
    summary = ", ".join(f"{status} {count}" for status, count in sorted(jobs.items()))
    print(f"Jobs:     {summary or 'none yet'}")
    if payload["resumable"]:
        print(f"\n{payload['resumable']} job(s) can be resumed with `dossier resume`")
    return 0


def _resume(config: Config, client: EdgarClient | None) -> int:
    with open_store(config.store_path) as conn:
        queue = JobQueue(conn, output_dir=config.output_dir)
        queue.reclaim_stale()
        pending = queue.resumable()
        if not pending:
            print("Nothing to resume.")
            return 0

        edgar = _client(client)
        print(f"Resuming {len(pending)} job(s)")
        failures = 0
        for job in pending:
            if job.job_type != "ingest_filer":
                print(f"  {job.job_id}: no handler for {job.job_type}, leaving it")
                continue
            line = _ingest_one(queue, conn, edgar, job.inputs["cik"], force=False)
            if "failed" in line:
                failures += 1
            print(line)
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
