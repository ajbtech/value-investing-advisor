-- Companies the screener flagged as of a date. `payload` is the candidate's full entry
-- in the screener's output contract: which screens flagged it and at what rank, why,
-- and every raw input with the filing it came from. The analysis layer is handed these
-- entries and nothing else, so each one has to stand on its own.
--
-- Re-screening the same date replaces that date's rows: the answer can change as more
-- filers are ingested, and the latest run is the one that counts.

CREATE TABLE candidate (
    as_of            TEXT    NOT NULL,
    cik              INTEGER NOT NULL REFERENCES filer(cik),
    screener_version TEXT    NOT NULL,
    screens          TEXT    NOT NULL,
    flag_reason      TEXT    NOT NULL,
    payload          TEXT    NOT NULL,
    created_at       TEXT    NOT NULL,
    PRIMARY KEY (as_of, cik)
);
