-- Every unit of work is a durable, idempotent job row. Nothing of value lives in memory,
-- and no session is assumed to finish what it starts.

CREATE TABLE job (
    job_id          INTEGER PRIMARY KEY,
    job_type        TEXT    NOT NULL,
    -- hash of (job_type, inputs, prompt_version, model_id). Filings are immutable once
    -- filed, so this is a content address and a completed job is valid forever.
    idempotency_key TEXT    NOT NULL UNIQUE,
    inputs          TEXT    NOT NULL,
    prompt_version  TEXT,
    model_id        TEXT,
    status          TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'running', 'done', 'failed')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    output_path     TEXT,
    cost_tokens     INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    created_at      TEXT    NOT NULL,
    started_at      TEXT,
    -- Null until the output is durably on disk. Never set in the same step as the write.
    finished_at     TEXT
);

-- The resume query: status IN ('pending','failed') AND attempts < 3.
CREATE INDEX job_resumable ON job (status, attempts);
CREATE INDEX job_by_type ON job (job_type, status);
