---
name: dossier
description: Drive the dossier value-investing pipeline from a conversation — ingest SEC filings, check what is in the store, resume interrupted work, and present results. Use whenever the user asks about screening filers, a company's fundamentals as of a date, what the store holds, or wants a dossier, screen or valuation run or explained. Also use when they ask to resume, retry or check on pipeline work.
---

# Driving the dossier pipeline

This repository is a research pipeline that reads SEC filings and produces cited
value-investing dossiers. The CLI is the interface you drive; the conversation is the
interface the user sees. Read `CLAUDE.md` for the invariants before changing anything.

## The one rule that matters most here

**Never characterise a finding you have not read, and never paraphrase a number.**

The pipeline validates every quoted claim against its source filing before it enters a
dossier. That validator protects the dossier's contents. It does not protect your
summary of them. If you restate a figure loosely, or describe what a filing "suggests"
without quoting it, you have added an unvalidated layer to a system built specifically
to have none.

So: quote, cite the accession number, and give the filing URL. When you are unsure
whether the store actually contains something, run the command and look rather than
inferring from context. "I don't have that yet" is always a better answer than a
plausible one.

## Before anything touches EDGAR

The SEC requires a declared `User-Agent` naming a human and an email:

```
export EDGAR_USER_AGENT="Jane Doe jane@example.com"
```

Without it the CLI exits 2 with an explanation, before any request goes out. If you hit
that, tell the user to set it rather than trying to work around it.

**Cloud sessions cannot reach `sec.gov`.** The egress proxy returns 403 on CONNECT. Any
command that fetches from EDGAR will fail in a cloud session — it has to run on the
user's own machine. Everything that only reads the local store works fine anywhere.

## Commands

Every command takes `--json` and emits parseable output on stdout with nothing else.
**Always pass `--json`** — the human-readable form is for the user's own terminal.

| Command | What it does |
| --- | --- |
| `dossier ingest --cik N --json` | Fetch one filer from EDGAR into the store. Repeatable. |
| `dossier ingest --limit N --json` | Fetch the first N filers from the ticker map. |
| `dossier ingest ... --force --json` | Re-run filers already ingested. |
| `dossier status --json` | What the store holds and what work is pending. |
| `dossier resume --json` | Retry everything pending or failed. |

Run them with `uv run dossier ...` so the project's own environment is used.

Exit codes: `0` success, `1` some work failed (the JSON says which), `2` a usage or
configuration problem (the message is on stderr).

### Reading the output

`ingest` and `resume` return a `results` array, one entry per filer, each with a
`status` of `ingested`, `cached`, `failed` or `skipped`. A `cached` result is not a
no-op to apologise for — it means the work was already done and correctly skipped.
`failed` entries stay resumable; suggest `dossier resume` rather than re-running ingest.

## Workflows

**"What's in the store?"** — `dossier status --json`, then report filers, filings, facts
and any resumable jobs. If `resumable` is above zero, say so and offer to resume.

**"Ingest Apple"** — CIKs are what the CLI takes, not tickers. Apple is 320193. If the
user gives a ticker you are not certain of, ingest by CIK only once you have confirmed
it; do not guess a CIK.

**"Something died halfway"** — `dossier resume --json`. The job table makes this safe:
work already completed is skipped, and an interrupted job loses at most one filer.

**"What was knowable on date X?"** — this is the heart of the system. All fact reads go
through `dossier.asof.AsOfView`, which filters `filed_date <= as_of`. There is no CLI
command for ad-hoc queries yet, so use a short Python snippet against the store, and
never read the `fact` table by any other path — a test enforces that and will fail.

## Presenting results

Prefer a short answer in chat. Reach for an artifact when the output is a document the
user will want to keep or re-read — a dossier, a screen with its reasons, a valuation
with its assumptions. Those carry citations, and citations survive better on a page than
in a scrollback.

When you render one, every factual claim keeps its accession number and filing URL. A
claim whose citation you cannot produce does not go in.

## What not to do

- Do not add price charts, watchlists, alerts, or anything real-time. They are out of
  scope permanently; `CLAUDE.md` explains why.
- Do not present pipeline output as a recommendation to buy or sell. This is a research
  tool and makes no individualised suggestions.
- Do not run a large `--limit` ingest without telling the user roughly what it will cost
  in time and requests. The SEC limit is 10 requests a second.
