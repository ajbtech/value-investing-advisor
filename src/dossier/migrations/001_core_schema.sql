-- Core point-in-time store.
--
-- The design rule this schema exists to enforce: facts are stored by when they became
-- public, not when they were earned. `fact.filed_date` is the column that separates an
-- honest backtest from a fantasy one, and it is NOT NULL for that reason.

CREATE TABLE filer (
    cik           INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL,
    ticker        TEXT,
    exchange      TEXT,
    sic           TEXT,
    -- Filers who fail stop filing. Keeping them here, with a terminal status, is what
    -- stops the universe from quietly deleting its own failures.
    status        TEXT    NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'deregistered', 'delisted_for_cause', 'acquired')),
    status_date   TEXT,
    first_seen    TEXT,
    last_filing_date TEXT
);

CREATE TABLE filing (
    accession_no    TEXT PRIMARY KEY,
    cik             INTEGER NOT NULL REFERENCES filer(cik),
    form_type       TEXT    NOT NULL,
    filed_date      TEXT    NOT NULL,
    period_end      TEXT,
    primary_doc_url TEXT,
    fiscal_year     INTEGER,
    fiscal_period   TEXT
);

CREATE INDEX filing_by_filer ON filing (cik, filed_date);
CREATE INDEX filing_by_form ON filing (form_type, filed_date);

-- One row per (fact, reporting filing). A restatement arrives under a new accession
-- number and is inserted alongside the original — never over it. `period_start` is ''
-- for instant facts rather than NULL, so the primary key actually constrains.
CREATE TABLE fact (
    accession_no  TEXT    NOT NULL REFERENCES filing(accession_no),
    cik           INTEGER NOT NULL REFERENCES filer(cik),
    tag           TEXT    NOT NULL,
    unit          TEXT    NOT NULL,
    period_start  TEXT    NOT NULL DEFAULT '',
    period_end    TEXT    NOT NULL,
    value         REAL    NOT NULL,
    fiscal_year   INTEGER,
    fiscal_period TEXT,
    form_type     TEXT,
    -- The critical column. Every downstream read filters on filed_date <= as_of.
    filed_date    TEXT    NOT NULL,
    PRIMARY KEY (accession_no, tag, unit, period_start, period_end)
);

-- The index the as-of gateway reads through.
CREATE INDEX fact_as_of ON fact (cik, tag, filed_date);

CREATE TABLE document_section (
    accession_no          TEXT NOT NULL REFERENCES filing(accession_no),
    item                  TEXT NOT NULL,
    text                  TEXT NOT NULL,
    -- 10-K item boundaries are inconsistent across filers and years. Recording how
    -- confident the extractor was lets the analysis layer refuse a bad parse.
    extraction_confidence REAL,
    char_count            INTEGER,
    extracted_at          TEXT,
    PRIMARY KEY (accession_no, item)
);

CREATE TABLE dossier (
    cik            INTEGER NOT NULL REFERENCES filer(cik),
    run_date       TEXT    NOT NULL,
    as_of          TEXT    NOT NULL,
    payload        TEXT    NOT NULL,
    prompt_version TEXT,
    model_id       TEXT,
    PRIMARY KEY (cik, run_date)
);
