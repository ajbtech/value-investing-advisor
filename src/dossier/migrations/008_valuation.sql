-- A valuation is a range with its assumptions attached, never a number on its own.
--
-- `payload` holds the whole thing: the exact figures from the store with the filings
-- they came from, every assumption as a bear/base/bull triple with its written
-- justification, the three resulting values, what today's price already implies, and
-- the buy threshold. A valuation whose assumptions have been separated from it is a
-- number nobody can argue with, which is the opposite of the point.
--
-- Re-running a date replaces it: assumptions get revised as findings accumulate, and
-- the latest run is the one that counts. `valuation_version` is part of the key so a
-- change to the arithmetic produces a new row rather than overwriting the old answer.

CREATE TABLE valuation (
    cik               INTEGER NOT NULL REFERENCES filer(cik),
    as_of             TEXT    NOT NULL,
    valuation_version TEXT    NOT NULL,
    model             TEXT,
    payload           TEXT    NOT NULL,
    created_at        TEXT    NOT NULL,
    PRIMARY KEY (cik, as_of, valuation_version)
);
