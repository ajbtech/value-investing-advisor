"""EDGAR JSON into the point-in-time store.

One detail of the `companyfacts` shape is what makes this whole design possible on free
data: every datapoint carries its own `filed` date and the `accn` that reported it. So
a restatement arrives as an additional datapoint rather than as an edit, and the store
can answer "what was knowable on this date" without a paid point-in-time feed.

Ingest only writes. Reads go through `dossier.asof`.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field, replace

#: The tags the screens actually use. `companyfacts` carries several thousand; ingesting
#: the rest costs a weekend and tens of gigabytes for nothing. Widen this later — you
#: cannot un-blow a weekend on a full ingest you did not need.
SCREEN_TAGS: frozenset[str] = frozenset(
    {
        # Income statement
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "GrossProfit",
        "OperatingIncomeLoss",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "NetIncomeLoss",
        "EarningsPerShareDiluted",
        "InterestExpense",
        "IncomeTaxExpenseBenefit",
        "ResearchAndDevelopmentExpense",
        # Balance sheet
        "Assets",
        "AssetsCurrent",
        "Liabilities",
        "LiabilitiesCurrent",
        "LiabilitiesAndStockholdersEquity",
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "CashAndCashEquivalentsAtCarryingValue",
        "ShortTermInvestments",
        "MarketableSecuritiesCurrent",
        "AccountsReceivableNetCurrent",
        "InventoryNet",
        "PropertyPlantAndEquipmentNet",
        "Goodwill",
        "IntangibleAssetsNetExcludingGoodwill",
        "LongTermDebtNoncurrent",
        "LongTermDebt",
        "LongTermDebtCurrent",
        "DebtCurrent",
        "OperatingLeaseLiabilityNoncurrent",
        "RetainedEarningsAccumulatedDeficit",
        # Cash flow — owner earnings lives here
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInInvestingActivities",
        "NetCashProvidedByUsedInFinancingActivities",
        # Capital expenditure has no single element, and filers migrate between them and
        # never migrate back: American Electric Power last used the standard tag in 2020,
        # MasTec in 2011. Neither stopped spending. Ingesting only the first left 83 of
        # 289 eligible filers with no capex at all, which silently shrank the owner
        # earnings screen by a third of its universe.
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsForCapitalImprovements",
        "PaymentsToAcquireOtherPropertyPlantAndEquipment",
        "PaymentsToAcquireMachineryAndEquipment",
        # Capitalized software is capital spending the property elements leave out.
        # Teladoc's free cash flow deducts $118.6M of it; the screen counted $8.9M.
        "PaymentsToDevelopSoftware",
        "PaymentsForSoftware",
        # A different cost measure, read last and for Piotroski's margin test only.
        "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
        # Same fragility, three more times. Gross profit was missing for 63 of 289
        # eligible filers, which cost every one of them a Piotroski score, because a
        # filer using `CostOfGoodsSold` or `CostOfServices` matched nothing. And since
        # ASC 842 many filers report property under the finance-lease element rather
        # than `PropertyPlantAndEquipmentNet`, which cost 53 filers a Magic Formula rank.
        "CostOfGoodsSold",
        "CostOfServices",
        "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "ShareBasedCompensation",
        "PaymentsOfDividends",
        "PaymentsForRepurchaseOfCommonStock",
        # Share counts
        "CommonStockSharesOutstanding",
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
        "EntityCommonStockSharesOutstanding",
    }
)


def tags_version(tags) -> str:
    """A short fingerprint of a tag set, for the ingest job's idempotency key.

    A completed ingest is only complete for the tags it fetched. Keying on the tag set
    means widening it re-runs ingest for every filer instead of serving a cache that
    predates the new tags.
    """
    return hashlib.sha256("\n".join(sorted(tags)).encode("utf-8")).hexdigest()[:12]


INGEST_VERSION = tags_version(SCREEN_TAGS)

#: Job types, named once. `resume` dispatches on these, and a mistyped one would leave
#: failed jobs that nothing ever retries.
INGEST_JOB = "ingest_filer"
BULK_FACTS_JOB = "ingest_facts_bulk"


@dataclass(frozen=True)
class FilerRecord:
    cik: int
    name: str
    ticker: str | None = None
    exchange: str | None = None
    sic: str | None = None
    last_filing_date: str | None = None
    #: The earliest filing we know of. `first_seen` in the store, and what the as-of
    #: universe filters on, so it must never be the most recent filing.
    first_filing_date: str | None = None


@dataclass(frozen=True)
class FilingRecord:
    accession_no: str
    cik: int
    form_type: str
    filed_date: str
    period_end: str | None = None
    primary_doc_url: str | None = None


@dataclass(frozen=True)
class FactRecord:
    accession_no: str
    cik: int
    tag: str
    unit: str
    period_start: str
    period_end: str
    value: float
    filed_date: str
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    form_type: str | None = None


@dataclass
class IngestResult:
    cik: int
    filings: int = 0
    facts: int = 0
    filings_inserted: int = 0
    facts_inserted: int = 0
    stub_filings: int = 0
    skipped_tags: set[str] = field(default_factory=set)


def _first(values: list | None) -> str | None:
    return values[0] if values else None


def archive_url(cik: int, accession_no: str, document: str) -> str:
    """EDGAR's archive path wants the accession number without its dashes."""
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{accession_no.replace('-', '')}/{document}"
    )


def parse_submissions(doc: dict) -> tuple[FilerRecord, list[FilingRecord]]:
    """Read a `submissions` document into a filer and its filings.

    `filings.recent` is column-oriented — parallel arrays, not records — so the
    transpose has to be exact. Getting it wrong pairs each filing with a neighbour's
    date, which is the kind of bug that looks like data and not like a crash.
    """
    cik = int(doc["cik"])
    recent = doc.get("filings", {}).get("recent", {})
    accessions = recent.get("accessionNumber", [])

    def column(name: str) -> list:
        values = recent.get(name, [])
        return list(values) + [None] * (len(accessions) - len(values))

    forms = column("form")
    filed_dates = column("filingDate")
    report_dates = column("reportDate")
    documents = column("primaryDocument")

    filings = [
        FilingRecord(
            accession_no=accession,
            cik=cik,
            form_type=forms[i],
            filed_date=filed_dates[i],
            period_end=report_dates[i] or None,
            primary_doc_url=archive_url(cik, accession, documents[i]) if documents[i] else None,
        )
        for i, accession in enumerate(accessions)
    ]

    filer = FilerRecord(
        cik=cik,
        name=doc.get("name") or doc.get("entityName") or "",
        ticker=_first(doc.get("tickers")),
        exchange=_first(doc.get("exchanges")),
        sic=doc.get("sic") or None,
        last_filing_date=max((f.filed_date for f in filings if f.filed_date), default=None),
        first_filing_date=min((f.filed_date for f in filings if f.filed_date), default=None),
    )
    return filer, filings


def parse_company_facts(
    doc: dict, tags: set[str] | frozenset[str] = SCREEN_TAGS, cik: int | None = None
) -> list[FactRecord]:
    """Read a `companyfacts` document into facts, each stamped with its filing date.

    A datapoint with no `start` is an instant (a balance-sheet item). Its period_start
    is stored as '' rather than NULL, so the primary key that prevents duplicate facts
    actually constrains — in SQLite a NULL in a primary key does not.
    """
    # Not every `companyfacts` document carries a `cik`. Three closed-end funds in a
    # live batch returned only `entityName` and `facts`, reporting under the `cef`
    # taxonomy, and failed here. The CIK the caller asked for is the authority; the
    # document's own is a cross-check, and a mismatch is refused rather than trusted,
    # because attributing one company's figures to another is worse than failing.
    stated = doc.get("cik")
    if stated is not None and cik is not None and int(stated) != int(cik):
        raise ValueError(
            f"companyfacts for CIK {cik} describes a different filer ({int(stated)}). "
            "Refusing to attribute one company's figures to another."
        )
    if cik is None:
        if stated is None:
            raise ValueError(
                "companyfacts carries no cik and none was supplied, so these facts cannot "
                "be attributed to a filer."
            )
        cik = stated
    cik = int(cik)
    facts: list[FactRecord] = []
    for taxonomy in doc.get("facts", {}).values():
        for tag, body in taxonomy.items():
            if tag not in tags:
                continue
            for unit, datapoints in body.get("units", {}).items():
                for point in datapoints:
                    if point.get("filed") is None or point.get("accn") is None:
                        # Without a filing date a fact cannot be placed in time, and a
                        # fact that cannot be placed in time is worse than no fact.
                        continue
                    facts.append(
                        FactRecord(
                            accession_no=point["accn"],
                            cik=cik,
                            tag=tag,
                            unit=unit,
                            period_start=point.get("start") or "",
                            period_end=point["end"],
                            value=float(point["val"]),
                            filed_date=point["filed"],
                            fiscal_year=point.get("fy"),
                            fiscal_period=point.get("fp"),
                            form_type=point.get("form"),
                        )
                    )
    return facts


def _upsert_filer(conn: sqlite3.Connection, filer: FilerRecord) -> None:
    """Write the filer, leaving any terminal status alone.

    A filer recorded as having failed must not be quietly reactivated by a later bulk
    ingest — that is survivorship bias coming back in through the side door.
    """
    conn.execute(
        """
        INSERT INTO filer (cik, name, ticker, exchange, sic, first_seen, last_filing_date)
        VALUES (:cik, :name, :ticker, :exchange, :sic, :first_filing_date, :last_filing_date)
        ON CONFLICT(cik) DO UPDATE SET
            name = excluded.name,
            ticker = excluded.ticker,
            exchange = excluded.exchange,
            sic = excluded.sic,
            last_filing_date = MAX(
                COALESCE(excluded.last_filing_date, ''), COALESCE(filer.last_filing_date, '')
            ),
            first_seen = MIN(
                COALESCE(filer.first_seen, excluded.first_seen),
                COALESCE(excluded.first_seen, filer.first_seen)
            )
        """,
        {
            "cik": filer.cik,
            "name": filer.name,
            "ticker": filer.ticker,
            "exchange": filer.exchange,
            "sic": filer.sic,
            "last_filing_date": filer.last_filing_date,
            "first_filing_date": filer.first_filing_date,
        },
    )


def _insert_filing(conn: sqlite3.Connection, filing: FilingRecord) -> bool:
    """Insert a filing, filling in details a stub row was missing. True if new."""
    cursor = conn.execute(
        """
        INSERT INTO filing (accession_no, cik, form_type, filed_date, period_end, primary_doc_url)
        VALUES (:accession_no, :cik, :form_type, :filed_date, :period_end, :primary_doc_url)
        ON CONFLICT(accession_no) DO UPDATE SET
            period_end = COALESCE(excluded.period_end, filing.period_end),
            primary_doc_url = COALESCE(excluded.primary_doc_url, filing.primary_doc_url)
        """,
        {
            "accession_no": filing.accession_no,
            "cik": filing.cik,
            "form_type": filing.form_type,
            "filed_date": filing.filed_date,
            "period_end": filing.period_end,
            "primary_doc_url": filing.primary_doc_url,
        },
    )
    return cursor.rowcount > 0 and cursor.lastrowid is not None


def ingest_filer(
    conn: sqlite3.Connection,
    submissions: dict,
    company_facts: dict | None = None,
    tags: set[str] | frozenset[str] = SCREEN_TAGS,
) -> IngestResult:
    """Write one filer, its filings and its facts. Safe to run again at any time.

    Facts use INSERT OR IGNORE against a primary key that includes the reporting
    accession number, so re-ingesting is a no-op while a genuine restatement — which
    arrives under a different accession — is added alongside the original.
    """
    filer, filings = parse_submissions(submissions)
    # The submissions document establishes who this is, so pass that on rather than
    # relying on companyfacts to repeat it — some documents do not.
    facts = parse_company_facts(company_facts, tags, cik=filer.cik) if company_facts else []
    result = IngestResult(cik=filer.cik, filings=len(filings), facts=len(facts))

    known = {filing.accession_no for filing in filings}
    stubs = _stub_filings(facts, known)
    result.stub_filings = len(stubs)

    # Stub filings reach back further than the submissions window, so the earliest
    # filing we know of may come from a fact rather than from `filings.recent`.
    earliest = min(
        (f.filed_date for f in [*filings, *stubs] if f.filed_date),
        default=filer.first_filing_date,
    )
    filer = replace(filer, first_filing_date=earliest)

    with conn:
        _upsert_filer(conn, filer)
        for filing in [*filings, *stubs]:
            if _insert_filing(conn, filing):
                result.filings_inserted += 1
        result.facts_inserted = _insert_facts(conn, facts)
    return result


def held_ciks(conn: sqlite3.Connection) -> list[int]:
    """Every filer in the store, in CIK order."""
    return [row[0] for row in conn.execute("SELECT cik FROM filer ORDER BY cik")]


def ingest_facts(
    conn: sqlite3.Connection,
    cik: int,
    company_facts: dict | None,
    tags: set[str] | frozenset[str] = SCREEN_TAGS,
) -> IngestResult:
    """Write one filer's facts from a `companyfacts` document, and nothing else.

    For the bulk ZIP, which carries facts for every filer on EDGAR but no filing index.
    The filer must already be in the store: deciding who is in the universe is the form
    index's job, and a facts file that could add filers would quietly take it over.
    """
    if conn.execute("SELECT 1 FROM filer WHERE cik = ?", (cik,)).fetchone() is None:
        raise ValueError(f"CIK {cik} is not in the store; ingest the filer before its facts")
    facts = parse_company_facts(company_facts, tags, cik=cik) if company_facts else []
    result = IngestResult(cik=cik, facts=len(facts))

    known = {
        row[0] for row in conn.execute("SELECT accession_no FROM filing WHERE cik = ?", (cik,))
    }
    stubs = _stub_filings(facts, known)
    result.stub_filings = len(stubs)

    with conn:
        for filing in stubs:
            if _insert_filing(conn, filing):
                result.filings_inserted += 1
        result.facts_inserted = _insert_facts(conn, facts)
        earliest = min((f.filed_date for f in facts), default=None)
        if earliest is not None:
            conn.execute(
                "UPDATE filer SET first_seen = MIN(COALESCE(first_seen, ?), ?) WHERE cik = ?",
                (earliest, earliest, cik),
            )
    return result


def _stub_filings(facts: list[FactRecord], known: set[str]) -> list[FilingRecord]:
    """A filing row for every accession a fact cites that the store has no row for.

    companyfacts reaches further back than the `recent` submissions window, so some
    facts cite filings we have no row for. Stub those in rather than dropping the facts,
    which keeps the foreign key honest and the history complete.
    """
    stubs: dict[str, FilingRecord] = {}
    for fact in facts:
        if fact.accession_no not in known and fact.accession_no not in stubs:
            stubs[fact.accession_no] = FilingRecord(
                accession_no=fact.accession_no,
                cik=fact.cik,
                form_type=fact.form_type or "UNKNOWN",
                filed_date=fact.filed_date,
            )
    return list(stubs.values())


def _insert_facts(conn: sqlite3.Connection, facts: list[FactRecord]) -> int:
    """Insert facts, never updating one. Returns how many were new."""
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO fact
            (accession_no, cik, tag, unit, period_start, period_end, value,
             fiscal_year, fiscal_period, form_type, filed_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                fact.accession_no,
                fact.cik,
                fact.tag,
                fact.unit,
                fact.period_start,
                fact.period_end,
                fact.value,
                fact.fiscal_year,
                fact.fiscal_period,
                fact.form_type,
                fact.filed_date,
            )
            for fact in facts
        ],
    )
    return conn.total_changes - before
