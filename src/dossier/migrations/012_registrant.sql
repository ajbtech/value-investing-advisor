-- Every annual report ever filed, from EDGAR's quarterly form index.
--
-- This is the universe the plan asks for: "build the universe as of the test date,
-- including filers that later deregistered". `company_tickers.json` cannot supply it,
-- because it lists what trades today — a company that failed in 2023 is simply absent,
-- and that is the population a value screen is most likely to have flagged.
--
-- One row per filing rather than a per-filer summary, so re-reading a quarter is
-- idempotent: the primary key makes a second pass a no-op instead of double-counting a
-- filer's history. The summary every caller actually wants is the view below.
--
-- A row claims nothing about a company except that it filed an annual report on a date.
-- No facts, no prices, no status. That separation is the point: it says precisely who the
-- pipeline has never heard of, which the ticker map could never say, and ingesting them
-- stays a separate and costly decision.

CREATE TABLE registrant_annual (
    cik         INTEGER NOT NULL,
    filed_date  TEXT    NOT NULL,
    form        TEXT    NOT NULL,
    name        TEXT,
    seen_at     TEXT    NOT NULL,
    PRIMARY KEY (cik, filed_date)
);

CREATE INDEX registrant_annual_by_date ON registrant_annual (filed_date);

CREATE VIEW registrant AS
SELECT cik,
       MAX(name)        AS name,
       MIN(filed_date)  AS first_annual,
       MAX(filed_date)  AS last_annual,
       COUNT(*)         AS annual_reports
FROM registrant_annual
GROUP BY cik;
