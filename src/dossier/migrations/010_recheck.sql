-- The quarterly re-check of each open thesis against its own falsification conditions.
--
-- One row per thesis version per run date, keeping the history: the point is to be able
-- to see when a condition started failing, not only that it fails now. `status` is
-- stored beside the payload so a breach can be found without parsing every report —
-- and `needs_a_human` is a status of its own, because a condition that could not be
-- checked must never be counted as one that passed.

CREATE TABLE recheck (
    cik            INTEGER NOT NULL REFERENCES filer(cik),
    as_of          TEXT    NOT NULL,
    thesis_version INTEGER NOT NULL,
    run_date       TEXT    NOT NULL,
    status         TEXT    NOT NULL
                   CHECK (status IN ('holding', 'breached', 'needs_a_human')),
    payload        TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    PRIMARY KEY (cik, as_of, thesis_version, run_date)
);
