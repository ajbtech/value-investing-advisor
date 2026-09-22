-- Findings from the analysis passes.
--
-- Every row carries the accession number of the filing its quote came from, because the
-- first non-negotiable is that no claim reaches a dossier without a traceable source.
-- A finding whose quote failed validation is never inserted; the count of those is kept
-- alongside the run, because a pipeline whose fabrication rate you cannot state is a
-- pipeline you cannot trust.

CREATE TABLE finding (
    finding_id         INTEGER PRIMARY KEY,
    run_key            TEXT    NOT NULL,
    cik                INTEGER NOT NULL REFERENCES filer(cik),
    accession_no       TEXT    NOT NULL REFERENCES filing(accession_no),
    item               TEXT    NOT NULL,
    change_type        TEXT    NOT NULL,
    quote              TEXT    NOT NULL,
    implication        TEXT    NOT NULL,
    severity           TEXT    NOT NULL,
    prior_accession_no TEXT,
    prior_quote        TEXT,
    pass               TEXT    NOT NULL,
    prompt_version     TEXT    NOT NULL,
    model              TEXT,
    created_at         TEXT    NOT NULL
);

CREATE INDEX finding_by_filer ON finding (cik, pass);
CREATE INDEX finding_by_run ON finding (run_key);

-- One row per analysis run, so the fabrication rate is a first-class metric rather than
-- something recomputed from whatever happens to have survived.
CREATE TABLE analysis_run (
    run_key          TEXT PRIMARY KEY,
    cik              INTEGER NOT NULL REFERENCES filer(cik),
    pass             TEXT    NOT NULL,
    prompt_version   TEXT    NOT NULL,
    model            TEXT,
    findings_kept    INTEGER NOT NULL,
    findings_dropped INTEGER NOT NULL,
    drop_reasons     TEXT,
    created_at       TEXT    NOT NULL
);
