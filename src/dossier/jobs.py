"""The job table: the only resume mechanism in the system.

A job is identified by what it would produce, not by when it was created, so a re-run
after an interrupted session skips straight to the unfinished work. Jobs never call
other jobs — a stage writes rows for the next stage to pick up.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MAX_ATTEMPTS = 3

#: A job claimed this long ago whose session never came back is assumed dead.
STALE_AFTER_SECONDS = 3600


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def idempotency_key(
    job_type: str,
    inputs: dict,
    prompt_version: str | None = None,
    model_id: str | None = None,
) -> str:
    """Content-address a unit of work.

    `sort_keys` makes the key insensitive to how the caller happened to order its
    inputs, so the same work requested two different ways is still one job.
    """
    payload = json.dumps(
        {
            "job_type": job_type,
            "inputs": inputs,
            "prompt_version": prompt_version,
            "model_id": model_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_durably(path: Path, payload: str | bytes) -> None:
    """Write a file and get it onto the disk before returning.

    Write to a temporary file in the destination directory, fsync it, rename it into
    place, then fsync the directory so the rename itself survives a power loss. Only
    once this returns may a caller mark the work done.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # os.replace, not Path.replace: it is the seam tests inject a failed rename at
        # (CLAUDE.md), and Path.replace reaching it is an implementation detail.
        os.replace(tmp, path)  # noqa: PTH105
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        # Windows refuses to open a directory as a file descriptor at all, raising
        # PermissionError. The rename above is already atomic there, so the directory
        # fsync is a durability nicety — not a step worth failing the write over.
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


@dataclass(frozen=True)
class Job:
    job_id: int
    job_type: str
    idempotency_key: str
    inputs: dict
    prompt_version: str | None
    model_id: str | None
    status: str
    attempts: int
    output_path: str | None
    cost_tokens: int
    error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        return cls(
            job_id=row["job_id"],
            job_type=row["job_type"],
            idempotency_key=row["idempotency_key"],
            inputs=json.loads(row["inputs"]),
            prompt_version=row["prompt_version"],
            model_id=row["model_id"],
            status=row["status"],
            attempts=row["attempts"],
            output_path=row["output_path"],
            cost_tokens=row["cost_tokens"],
            error=row["error"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    def read_output(self) -> str:
        if self.output_path is None:
            raise ValueError(f"job {self.job_id} has no output on disk")
        return Path(self.output_path).read_text(encoding="utf-8")


class JobQueue:
    """Durable work queue over the store's `job` table."""

    def __init__(self, conn: sqlite3.Connection, output_dir: Path | str) -> None:
        self.conn = conn
        self.output_dir = Path(output_dir)

    # -- reads ---------------------------------------------------------------

    def get(self, job_id: int) -> Job:
        row = self.conn.execute("SELECT * FROM job WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"no job {job_id}")
        return Job.from_row(row)

    def find(self, key: str) -> Job | None:
        row = self.conn.execute("SELECT * FROM job WHERE idempotency_key = ?", (key,)).fetchone()
        return Job.from_row(row) if row else None

    def completed(self, key: str) -> Job | None:
        """The check-before-spending lookup. Only a finished job is a cache hit."""
        job = self.find(key)
        return job if job is not None and job.status == "done" else None

    def resumable(self) -> list[Job]:
        rows = self.conn.execute(
            "SELECT * FROM job WHERE status IN ('pending', 'failed') AND attempts < ? "
            "ORDER BY job_id",
            (MAX_ATTEMPTS,),
        ).fetchall()
        return [Job.from_row(row) for row in rows]

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM job").fetchone()[0]

    def status_counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM job GROUP BY status")
        return {row["status"]: row["n"] for row in rows}

    # -- writes --------------------------------------------------------------

    def enqueue(
        self,
        job_type: str,
        inputs: dict,
        *,
        prompt_version: str | None = None,
        model_id: str | None = None,
    ) -> Job:
        """Register a unit of work, or return the existing job for the same key.

        Enqueueing work that is already done returns the finished job untouched, so
        callers can enqueue freely without reopening settled work.
        """
        key = idempotency_key(job_type, inputs, prompt_version, model_id)
        existing = self.find(key)
        if existing is not None:
            return existing
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO job (job_type, idempotency_key, inputs, prompt_version, "
                "model_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job_type,
                    key,
                    json.dumps(inputs, sort_keys=True),
                    prompt_version,
                    model_id,
                    _now(),
                ),
            )
        return self.get(cursor.lastrowid)

    def claim(self, job_type: str | None = None) -> Job | None:
        """Take the next eligible job and mark it running.

        The UPDATE ... WHERE status is still what we read is what makes this safe to
        call from two processes at once: the loser's update matches no rows.
        """
        while True:
            sql = "SELECT * FROM job WHERE status IN ('pending', 'failed') AND attempts < ? "
            params: list = [MAX_ATTEMPTS]
            if job_type is not None:
                sql += "AND job_type = ? "
                params.append(job_type)
            sql += "ORDER BY job_id LIMIT 1"

            row = self.conn.execute(sql, params).fetchone()
            if row is None:
                return None

            with self.conn:
                cursor = self.conn.execute(
                    "UPDATE job SET status = 'running', attempts = attempts + 1, "
                    "started_at = ? WHERE job_id = ? AND status = ?",
                    (_now(), row["job_id"], row["status"]),
                )
            if cursor.rowcount == 1:
                return self.get(row["job_id"])
            # Another worker took it between the select and the update; look again.

    def claim_by_key(self, key: str) -> Job | None:
        """Claim one named unit of work, rather than whatever comes next.

        A stage that already knows which filing it is processing wants this; `claim()`
        is for a worker draining the queue.
        """
        row = self.conn.execute(
            "SELECT * FROM job WHERE idempotency_key = ? AND status IN ('pending', 'failed') "
            "AND attempts < ?",
            (key, MAX_ATTEMPTS),
        ).fetchone()
        if row is None:
            return None
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE job SET status = 'running', attempts = attempts + 1, started_at = ? "
                "WHERE job_id = ? AND status = ?",
                (_now(), row["job_id"], row["status"]),
            )
        return self.get(row["job_id"]) if cursor.rowcount == 1 else None

    def reopen(self, job: Job) -> Job:
        """Make a settled job eligible again, for `--force`.

        Prompts get retuned and extractors get fixed, so cached work sometimes has to
        be redone deliberately. The attempt count resets too — otherwise a job that
        already failed twice comes back with one attempt left and gives up at once.
        """
        with self.conn:
            self.conn.execute(
                "UPDATE job SET status = 'pending', attempts = 0, output_path = NULL, "
                "error = NULL, started_at = NULL, finished_at = NULL WHERE job_id = ?",
                (job.job_id,),
            )
        return self.get(job.job_id)

    def finish(self, job: Job, payload: str | bytes, *, cost_tokens: int = 0) -> Job:
        """Write the output durably, and only then mark the job done."""
        path = self.output_dir / job.job_type / f"{job.idempotency_key}.json"
        write_durably(path, payload)
        with self.conn:
            self.conn.execute(
                "UPDATE job SET status = 'done', output_path = ?, cost_tokens = ?, "
                "error = NULL, finished_at = ? WHERE job_id = ?",
                (str(path), cost_tokens, _now(), job.job_id),
            )
        return self.get(job.job_id)

    def fail(self, job: Job, error: str) -> Job:
        with self.conn:
            self.conn.execute(
                "UPDATE job SET status = 'failed', error = ? WHERE job_id = ?",
                (error, job.job_id),
            )
        return self.get(job.job_id)

    def reclaim_stale(self, older_than_seconds: int = STALE_AFTER_SECONDS) -> int:
        """Return jobs stranded in 'running' by a session that never came back.

        Without this a session killed mid-job leaves work that no later session will
        ever pick up, which is exactly the failure the job table exists to prevent.
        """
        cutoff = datetime.now(UTC).timestamp() - older_than_seconds
        stranded = [
            row["job_id"]
            for row in self.conn.execute(
                "SELECT job_id, started_at FROM job WHERE status = 'running'"
            )
            if row["started_at"] is None
            or datetime.fromisoformat(row["started_at"]).timestamp() <= cutoff
        ]
        if not stranded:
            return 0
        with self.conn:
            self.conn.executemany(
                "UPDATE job SET status = 'failed', error = 'interrupted' WHERE job_id = ?",
                [(job_id,) for job_id in stranded],
            )
        return len(stranded)
