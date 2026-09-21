"""EDGAR JSON into the point-in-time store.

One detail of the `companyfacts` shape is what makes this whole design possible on free
data: every datapoint carries its own `filed` date and the `accn` that reported it. So
a restatement arrives as an additional datapoint rather than as an edit, and the store
can answer "what was knowable on this date" without a paid point-in-time feed.

Ingest only writes. Reads go through `dossier.asof`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

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
        "LongTermDebtCurrent",
        "DebtCurrent",
        "OperatingLeaseLiabilityNoncurrent",
        "RetainedEarningsAccumulatedDeficit",
        # Cash flow — owner earnings lives here
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInInvestingActivities",
        "NetCashProvidedByUsedInFinancingActivities",
        "PaymentsToAcquirePropertyPlantAndEquipment",
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


@dataclass(frozen=True)
class FilerRecord:
    cik: int
    name: str
    ticker: str | None = None
    exchange: str | None = None
    sic: str | None = None
    last_filing_date: str | None = None


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
    )
    return filer, filings


def parse_company_facts(
    doc: dict, tags: set[str] | frozenset[str] = SCREEN_TAGS
) -> list[FactRecord]:
    """Read a `companyfacts` document into facts, each stamped with its filing date.

    A datapoint with no `start` is an instant (a balance-sheet item). Its period_start
    is stored as '' rather than NULL, so the primary key that prevents duplicate facts
    actually constrains — in SQLite a NULL in a primary key does not.
    """
    cik = int(doc["cik"])
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
        VALUES (:cik, :name, :ticker, :exchange, :sic, :last_filing_date, :last_filing_date)
        ON CONFLICT(cik) DO UPDATE SET
            name = excluded.name,
            ticker = excluded.ticker,
            exchange = excluded.exchange,
            sic = excluded.sic,
            last_filing_date = MAX(
                COALESCE(excluded.last_filing_date, ''), COALESCE(filer.last_filing_date, '')
            ),
            first_seen = MIN(
                COALESCE(filer.first_seen, excluded.first_seen, ''),
                COALESCE(excluded.first_seen, '')
            )
        """,
        {
            "cik": filer.cik,
            "name": filer.name,
            "ticker": filer.ticker,
            "exchange": filer.exchange,
            "sic": filer.sic,
            "last_filing_date": filer.last_filing_date,
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
    facts = parse_company_facts(company_facts, tags) if company_facts else []
    result = IngestResult(cik=filer.cik, filings=len(filings), facts=len(facts))

    known = {filing.accession_no for filing in filings}
    # companyfacts reaches further back than the `recent` submissions window, so some
    # facts cite filings we have no row for. Stub those in rather than dropping the
    # facts, which keeps the foreign key honest and the history complete.
    stubs = {}
    for fact in facts:
        if fact.accession_no not in known and fact.accession_no not in stubs:
            stubs[fact.accession_no] = FilingRecord(
                accession_no=fact.accession_no,
                cik=fact.cik,
                form_type=fact.form_type or "UNKNOWN",
                filed_date=fact.filed_date,
            )
    result.stub_filings = len(stubs)

    with conn:
        _upsert_filer(conn, filer)
        for filing in [*filings, *stubs.values()]:
            if _insert_filing(conn, filing):
                result.filings_inserted += 1
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
        result.facts_inserted = conn.total_changes - before
    return result
