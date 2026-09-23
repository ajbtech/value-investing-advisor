-- The thesis and the bear case that tried to kill it.
--
-- Append-only, deliberately. A revision inserts a new version rather than replacing the
-- old one: the record of what was believed, and when, is the part that compounds, and a
-- thesis that can be quietly rewritten after the fact records nothing. The bear pass
-- attaches to the version it attacked — you do not get to delete the counterargument
-- once you have bought.

CREATE TABLE thesis (
    cik            INTEGER NOT NULL REFERENCES filer(cik),
    as_of          TEXT    NOT NULL,
    version        INTEGER NOT NULL,
    payload        TEXT    NOT NULL,
    prompt_version TEXT    NOT NULL,
    model          TEXT,
    created_at     TEXT    NOT NULL,
    PRIMARY KEY (cik, as_of, version)
);

-- One row per surviving bear point. Dropped points are not stored — a claim whose quote
-- is not in the filing is not evidence — but the count of them is, on the pass, because
-- the fabrication rate is a first-class metric here as everywhere else.
CREATE TABLE bear_point (
    cik            INTEGER NOT NULL REFERENCES filer(cik),
    as_of          TEXT    NOT NULL,
    thesis_version INTEGER NOT NULL,
    attacks        TEXT    NOT NULL,
    claim          TEXT    NOT NULL,
    accession_no   TEXT    NOT NULL REFERENCES filing(accession_no),
    item           TEXT    NOT NULL,
    quote          TEXT    NOT NULL,
    prompt_version TEXT    NOT NULL,
    model          TEXT,
    created_at     TEXT    NOT NULL
);

CREATE TABLE bear_pass (
    cik            INTEGER NOT NULL,
    as_of          TEXT    NOT NULL,
    thesis_version INTEGER NOT NULL,
    kept           INTEGER NOT NULL,
    dropped        INTEGER NOT NULL,
    drop_reasons   TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    PRIMARY KEY (cik, as_of, thesis_version)
);
