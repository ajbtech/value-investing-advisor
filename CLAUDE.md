# CLAUDE.md — invariants for this repository

This file exists so a session that never saw the build plan still builds the right thing.
It states the rules that are not negotiable. `STATE.md` says where the work currently stands.

## How we work

**Test-driven development, always.** Write the failing test first, watch it fail for the
right reason, then write the smallest code that makes it pass, then refactor. A commit that
adds behaviour without a test that would have failed before it is not finished work.

Commit after every job-sized step. An interrupted session should lose at most one
uncommitted step.

## The three non-negotiables

1. **No uncited claims.** Every factual statement in a dossier carries a filing URL and an
   accession number. A claim that cannot be traced to a source is a bug, not a nuance.
2. **No future knowledge.** Every fact is stamped with the date it became public, not the
   fiscal period it describes. Enforced in the schema and the read path, never in a prompt.
3. **Adversarial by default.** No thesis reaches the journal without a dedicated bear pass
   that tried to kill it.

## Data integrity rules, enforced in code

- **`filed_date <= as_of`, through one function.** Every read of the `fact` table goes
  through the single as-of gateway. There is no other read path. A test asserts that a known
  filing is invisible the day before it was filed, and it runs in CI.
- **`price_date <= as_of`, through the same gateway.** Prices carry the same lookahead
  risk as facts, and a second structural test keeps every read of `price` in
  `dossier.asof`. Stored closes are the price as traded that day: free sources adjust
  history for splits that happened later, and `dossier.prices` undoes that before
  anything is written.
- **Screens read materialised as-of tables, never `fact` or `price`.**
  `AsOfView.materialise()` writes what was knowable on the date into TEMP tables, and
  the screen views in `dossier.screens` read only those.
- **Never update a fact row in place.** `companyfacts` returns today's restated figures.
  Keep every version of every fact, keyed by the accession number that reported it.
  Restatements are systematically biased; overwriting them silently breaks any backtest.
- **Build the universe as of the test date**, including filers that later deregistered.
  Retain delisted CIKs with a terminal status. A delisting for cause is a total loss in
  historical evaluation, not a missing value.
- **Validate every quote.** A finding's quoted text is string-matched against the source
  filing before it enters a dossier. The failure rate is a first-class metric.

## The job-table contract

Every unit of work is a durable, idempotent job row. Nothing of value lives in memory.

- `idempotency_key = hash(job_type, inputs, prompt_version, model_id)`. Filings are
  immutable once filed, so a completed job is valid forever. Lean on this hard.
- **One job, one filing, one pass.** Never "analyze all 30 candidates" as a unit. A job that
  runs longer than a couple of minutes is too big to interrupt safely.
- **Check before spending.** Every job looks up its idempotency key first and returns the
  cached output on a hit.
- **Write then mark.** Output is written and fsynced to disk *before* status flips to
  `done`. The reverse order loses work on a crash, and loses it silently.
- **Resume is a query, not a protocol:**
  `SELECT * FROM job WHERE status IN ('pending','failed') AND attempts < 3`.
  There is no other resume mechanism, and so nothing to get out of sync.
- **Jobs never call other jobs.** A stage writes rows for the next stage to pick up.

## Interfaces

The CLI is the machine interface, not just a human one. It is what an agent drives, what
CI exercises, and what makes every stage independently testable — so it stays, even
though the primary way this tool is used is through a conversation with Claude.

- **Every command takes `--json`** and emits parseable output on stdout and nothing else.
  A stray progress line turns a parse into a guess. Errors go to stderr, always.
- A new subcommand inherits `--json` from the shared parent parser. Do not add one
  without it.
- `.claude/skills/dossier/SKILL.md` teaches a Claude session how to drive the CLI. Keep
  it current when commands change; a skill describing a flag that no longer exists is
  worse than no skill.

**Conversation is an unvalidated layer.** The quote validator protects what enters a
dossier. It does not protect a summary of that dossier in chat. Restating a figure
loosely, or describing what a filing "suggests" without quoting it, reintroduces exactly
the fabrication this design exists to prevent — one step downstream, where nothing
checks it. Quote, cite the accession number, link the filing. The dossier JSON is the
record; chat is a lens on it, never a replacement.

## The model runs through Claude Code, not an API key

The analysis passes are driven by a Claude Code session on an existing subscription.
There is no API key in this project, which is the point: a key that does not exist
cannot be published.

- `dossier analyze --prepare` writes a pass's input — the prompt and the sections it
  reads — as JSON. Claude produces the findings. `dossier analyze --load` validates
  every quote against the source filing and stores what survives.
- **The validator applies identically either way.** It never depended on where the text
  came from. "It is only for me" is the usual reason to drop a check like this, and it
  is not a reason: a tool that lies to one person is no better than one that lies to a
  thousand.
- `dossier.models` holds an API path — a provider interface, prices, a projected cost
  and a conservative ceiling — for work that must run unattended, such as the quarterly
  falsification re-check. It is not the default and needs no key until it is used.
- Support for other providers is not built. It existed so strangers could bring their
  own model, and there are no strangers.

## This repository is public; the tool is personal

The code is open. The audience is not. It is built for one person on their own machine,
is not offered as a service, and carries no obligation to support anyone else.

- **Nothing secret or personal is ever committed.** `tests/test_no_secrets.py` scans
  every tracked file for credentials and for real email addresses, and verifies its own
  patterns on each run — a guard that cannot fire reports all-clear, which is worse than
  no guard at all.
- Placeholder addresses use only the domains reserved for documentation
  (`example.com`, `example.org`, `.invalid`, `.test`). Allowlisting invented domains one
  at a time is how a check like that quietly stops working.
- The EDGAR `User-Agent` must carry a real name and email, and the SEC sees it on every
  request. It lives in an environment variable and must never reach a tracked file, a
  README example, or a public issue.

## Component boundaries

Each stage is a module with its own CLI entry point. Stages communicate *only* through the
store — never by importing each other.

| Command | Reads | Writes |
| --- | --- | --- |
| `dossier ingest` | EDGAR | `filer` + `filing` + `fact` |
| `dossier extract` | `filing` | `document_section` |
| `dossier prices` | Yahoo chart endpoint | `price` |
| `dossier screen` | `fact` + `price` | `candidate` |
| `dossier analyze --pass a --cik N` | `document_section` | `finding` |
| `dossier value --cik N` | `fact` + `finding` | `valuation` |
| `dossier serve` | everything | nothing |

## SEC EDGAR access rules

Ignore these and you get blocked:

- Maximum **10 requests per second** to sec.gov, and they monitor it.
- A declared `User-Agent` of the form `Name your@email.com` is **mandatory**. Omit it and you
  get an "Undeclared Automated Tool" error. Fail loudly and early on a missing or malformed
  one rather than letting the SEC block the user.
- Send `Accept-Encoding: gzip, deflate`.
- Prefer the nightly bulk ZIPs over per-company API calls wherever possible.

## Where things live

Code in this repo. Data on the user's machine, never inside the checkout.

| Item | Location |
| --- | --- |
| SQLite store, raw filings, extracted sections | Per-user data dir (`platformdirs`) |
| LLM analysis cache | Per-user data dir, worth backing up — the only artifact that costs money to recreate |
| Decision journal | User-chosen local dir or a private repo, never this public one |
| API keys | Gitignored `.env` |
| Code, migrations, prompt versions | This repo |
| Test fixtures (a few real filings) | This repo — small, needed for CI |
| Sample database | A GitHub Releases asset, to keep clones small |

## Git and branches

`main` is protected. Nothing is pushed to it directly — every change arrives by pull
request with `ci-green` passing.

- Agent sessions work on `claude/*` branches and push only there.
- Cloud sessions never hold a GitHub token. The credential is held by the egress proxy
  and scoped to this repository, so a session can use it but cannot read or copy it.
- The `ci-green` job in `.github/workflows/ci.yml` exists solely to give branch
  protection one stably-named check to require. Requiring the matrix jobs by name means
  four names that change whenever the matrix does, and a required check that no longer
  exists is silently skipped rather than enforced. Do not rename or remove `ci-green`
  without updating the protection rule; `tests/test_ci_workflow.py` guards its shape.

The reason all of this is in GitHub settings rather than in this file: an instruction to
an agent is not a control. This paragraph cannot stop a bad push. Branch protection can.

## Conventions

- `pathlib` everywhere; no shell-specific scripts. The primary dev machine is Windows and CI
  runs on both `windows-latest` and `ubuntu-latest`.
- `uv` for the toolchain.
- Dates are ISO `YYYY-MM-DD` strings at rest and `datetime.date` in memory.
- Accession numbers are stored in dashed form (`0000320193-24-000123`).
- Tests never touch the live SEC. Anything network-bound is tested against a fake transport;
  the handful of live tests are marked `@pytest.mark.network` and deselected by default.
- **Never monkeypatch `os.open` to raise `PermissionError` globally in a test.** On Windows,
  `tempfile._mkstemp_inner` reads that error as "a directory of this name already exists"
  and retries `TMP_MAX` times, stat-ing the filesystem twice per attempt; on Linux the same
  branch re-raises at once. A test that passes in a second locally then hangs for minutes on
  the Windows leg alone. Inject the failure at a narrower seam — `tempfile.mkstemp`,
  `os.replace` — instead.

## Scope discipline

Out of scope, deliberately and permanently: price-series signals, technical indicators,
momentum, autonomous order placement, any prediction over days or weeks, and position sizing
by an LLM. Requests for alerts, watchlists, charts or real-time data pull this toward the
trading bot the design exists to avoid.

The `price` table does not change this. A close exists to turn a share count into a market
cap on the as-of date, and for nothing else. Nothing here reads a price's movement, and a
feature that would is out of scope for the reasons above.

This is a research tool, not investment advice, and it makes no individualized
recommendations. Publishing the repository does not make it a product, and nothing here
should be written as though it were.
