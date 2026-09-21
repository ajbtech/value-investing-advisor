"""The job table is the only resume mechanism, so its contract is tested hard.

Four properties matter, and each maps to a rule in CLAUDE.md: a job is identified by
what it would produce (check before spending), output is durable before the row says
so (write then mark), resume is a plain query, and a job that has failed too often
stops being retried.
"""

import json
from pathlib import Path

import pytest

from dossier.jobs import MAX_ATTEMPTS, JobQueue, idempotency_key, write_durably
from dossier.store import open_store


@pytest.fixture
def queue(tmp_path):
    with open_store(tmp_path / "test.sqlite") as conn:
        yield JobQueue(conn, output_dir=tmp_path / "out")


class TestIdempotencyKey:
    """Filings are immutable once filed, which makes the key a content address: a
    completed job is valid forever, so a re-run costs nothing for work already done."""

    def test_is_stable_across_calls(self):
        args = ("pass_a", {"cik": 320193, "accession": "0000320193-24-000123"})
        assert idempotency_key(*args) == idempotency_key(*args)

    def test_ignores_input_ordering(self):
        a = idempotency_key("pass_a", {"cik": 320193, "accession": "x"})
        b = idempotency_key("pass_a", {"accession": "x", "cik": 320193})
        assert a == b

    def test_changes_with_the_inputs(self):
        a = idempotency_key("pass_a", {"cik": 320193})
        b = idempotency_key("pass_a", {"cik": 789019})
        assert a != b

    def test_changes_with_the_job_type(self):
        assert idempotency_key("pass_a", {"cik": 1}) != idempotency_key("pass_b", {"cik": 1})

    def test_changes_with_the_prompt_version(self):
        """When results shift you need to know whether the world changed or the prompt did."""
        a = idempotency_key("pass_a", {"cik": 1}, prompt_version="v1")
        b = idempotency_key("pass_a", {"cik": 1}, prompt_version="v2")
        assert a != b

    def test_changes_with_the_model_id(self):
        a = idempotency_key("pass_a", {"cik": 1}, model_id="claude-opus-5")
        b = idempotency_key("pass_a", {"cik": 1}, model_id="claude-haiku-4-5-20251001")
        assert a != b


class TestEnqueue:
    def test_returns_a_pending_job(self, queue):
        job = queue.enqueue("ingest_filer", {"cik": 320193})
        assert job.status == "pending"
        assert job.attempts == 0
        assert job.finished_at is None

    def test_is_idempotent(self, queue):
        first = queue.enqueue("ingest_filer", {"cik": 320193})
        second = queue.enqueue("ingest_filer", {"cik": 320193})
        assert first.job_id == second.job_id
        assert queue.count() == 1

    def test_distinct_inputs_make_distinct_jobs(self, queue):
        queue.enqueue("ingest_filer", {"cik": 320193})
        queue.enqueue("ingest_filer", {"cik": 789019})
        assert queue.count() == 2

    def test_round_trips_the_inputs(self, queue):
        job = queue.enqueue("ingest_filer", {"cik": 320193, "tags": ["Revenues"]})
        assert queue.get(job.job_id).inputs == {"cik": 320193, "tags": ["Revenues"]}


class TestCheckBeforeSpending:
    def test_a_completed_job_is_found_by_its_key(self, queue):
        job = queue.enqueue("pass_a", {"cik": 320193}, prompt_version="v1")
        queue.claim()
        queue.finish(job, json.dumps({"findings": []}), cost_tokens=1234)

        cached = queue.completed(idempotency_key("pass_a", {"cik": 320193}, prompt_version="v1"))
        assert cached is not None
        assert json.loads(cached.read_output()) == {"findings": []}

    def test_a_pending_job_is_not_a_cache_hit(self, queue):
        queue.enqueue("pass_a", {"cik": 320193})
        assert queue.completed(idempotency_key("pass_a", {"cik": 320193})) is None

    def test_a_different_prompt_version_is_not_a_cache_hit(self, queue):
        job = queue.enqueue("pass_a", {"cik": 320193}, prompt_version="v1")
        queue.claim()
        queue.finish(job, "{}")
        assert queue.completed(idempotency_key("pass_a", {"cik": 320193}, "v2")) is None

    def test_re_enqueueing_a_completed_job_does_not_reopen_it(self, queue):
        job = queue.enqueue("pass_a", {"cik": 320193})
        queue.claim()
        queue.finish(job, "{}")
        again = queue.enqueue("pass_a", {"cik": 320193})
        assert again.status == "done"
        assert queue.claim() is None


class TestClaim:
    def test_marks_running_and_counts_the_attempt(self, queue):
        queue.enqueue("ingest_filer", {"cik": 320193})
        job = queue.claim()
        assert job.status == "running"
        assert job.attempts == 1
        assert job.started_at is not None

    def test_returns_none_when_there_is_nothing_to_do(self, queue):
        assert queue.claim() is None

    def test_does_not_hand_out_the_same_job_twice(self, queue):
        queue.enqueue("ingest_filer", {"cik": 320193})
        queue.claim()
        assert queue.claim() is None

    def test_can_be_filtered_by_job_type(self, queue):
        queue.enqueue("ingest_filer", {"cik": 320193})
        queue.enqueue("pass_a", {"cik": 320193})
        job = queue.claim(job_type="pass_a")
        assert job.job_type == "pass_a"

    def test_retries_a_failed_job(self, queue):
        """Resume is a query: a failed job is simply eligible again."""
        queue.enqueue("ingest_filer", {"cik": 320193})
        job = queue.claim()
        queue.fail(job, "connection reset")
        retried = queue.claim()
        assert retried is not None
        assert retried.job_id == job.job_id
        assert retried.attempts == 2

    def test_gives_up_after_the_attempt_limit(self, queue):
        queue.enqueue("ingest_filer", {"cik": 320193})
        for _ in range(MAX_ATTEMPTS):
            job = queue.claim()
            assert job is not None
            queue.fail(job, "still broken")
        assert queue.claim() is None

    def test_a_job_interrupted_while_running_is_recoverable(self, queue):
        """A session cut off mid-job leaves a 'running' row behind. The next session
        must be able to pick it up, or the work is stranded forever."""
        queue.enqueue("ingest_filer", {"cik": 320193})
        queue.claim()  # session dies here, nothing marks it failed
        assert queue.reclaim_stale(older_than_seconds=0) == 1
        assert queue.claim() is not None


class TestWriteThenMark:
    """Output is durable before the row says so. The reverse order loses work on a
    crash and, worse, loses it silently."""

    def test_finish_writes_the_output_then_marks_done(self, queue):
        job = queue.enqueue("pass_a", {"cik": 320193})
        queue.claim()
        finished = queue.finish(job, json.dumps({"findings": ["x"]}), cost_tokens=99)

        assert finished.status == "done"
        assert finished.finished_at is not None
        assert finished.cost_tokens == 99
        assert json.loads(finished.read_output()) == {"findings": ["x"]}

    def test_a_job_is_not_marked_done_if_its_output_cannot_be_written(self, queue, monkeypatch):
        job = queue.enqueue("pass_a", {"cik": 320193})
        queue.claim()

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("dossier.jobs.write_durably", boom)
        with pytest.raises(OSError):
            queue.finish(job, "{}")

        assert queue.get(job.job_id).status != "done"
        assert queue.get(job.job_id).finished_at is None

    def test_finished_at_is_null_until_done(self, queue):
        job = queue.enqueue("pass_a", {"cik": 320193})
        assert queue.get(job.job_id).finished_at is None
        claimed = queue.claim()
        assert queue.get(claimed.job_id).finished_at is None


class TestWriteDurablyAcrossPlatforms:
    """`write_durably` fsyncs the containing directory so the rename survives a power
    loss. Windows will not open a directory as a file descriptor at all, and that
    refusal must not fail the write: the rename is already atomic there, so the
    directory fsync is a durability nicety rather than a step worth losing work over.
    """

    def test_survives_a_platform_that_refuses_to_open_a_directory(self, tmp_path, monkeypatch):
        import os

        real_open = os.open

        def refuse_directories(path, flags, *args, **kwargs):
            # Only directory opens are refused. mkstemp opens a file that does not
            # exist yet, so it is unaffected — which matters, because a PermissionError
            # reaching mkstemp sends it into a TMP_MAX-long retry loop on Windows.
            if isinstance(path, (str, bytes, os.PathLike)) and Path(os.fsdecode(path)).is_dir():
                raise PermissionError(13, "Permission denied")  # what Windows raises
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", refuse_directories)

        dest = tmp_path / "out" / "pass_a" / "result.json"
        write_durably(dest, '{"findings": []}')
        assert dest.read_text(encoding="utf-8") == '{"findings": []}'

    def test_still_reports_a_genuine_write_failure(self, tmp_path, monkeypatch):
        """Tolerating the directory fsync must not swallow a real failure to write.

        The failure is injected at mkstemp rather than by refusing os.open globally.
        A global refusal looks tidier but is a trap: tempfile treats PermissionError
        as "a directory of that name exists" and retries TMP_MAX times on Windows,
        which turns a fast test into a multi-minute hang on that platform alone.
        """
        import tempfile

        def no_space(*args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(tempfile, "mkstemp", no_space)

        dest = tmp_path / "out" / "result.json"
        with pytest.raises(OSError):
            write_durably(dest, "{}")

        assert not dest.exists()

    def test_leaves_nothing_behind_when_the_rename_fails(self, tmp_path, monkeypatch):
        """The output is written to a temp file and renamed into place. If the rename
        fails, neither a partial output nor an orphaned temp file may survive — a
        half-written file that looked complete would be worse than no file at all."""
        import os

        def fail_rename(*args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "replace", fail_rename)

        dest = tmp_path / "out" / "result.json"
        with pytest.raises(OSError):
            write_durably(dest, '{"findings": ["a", "b"]}')

        assert not dest.exists()
        assert list((tmp_path / "out").iterdir()) == []


class TestResume:
    def test_resumable_is_the_documented_query(self, queue):
        """SELECT * FROM job WHERE status IN ('pending','failed') AND attempts < 3.

        Distinct job types keep the claim order deterministic, so each job can be
        driven to exactly the state it is meant to be in.
        """
        pending = queue.enqueue("still_pending", {"cik": 1})
        failed = queue.enqueue("failed_once", {"cik": 2})
        done = queue.enqueue("already_done", {"cik": 3})
        exhausted = queue.enqueue("gave_up", {"cik": 4})

        queue.fail(queue.claim(job_type="failed_once"), "connection reset")
        queue.finish(queue.claim(job_type="already_done"), "{}")
        for _ in range(MAX_ATTEMPTS):
            queue.fail(queue.claim(job_type="gave_up"), "still broken")

        ids = {job.job_id for job in queue.resumable()}
        assert ids == {pending.job_id, failed.job_id}
        assert done.job_id not in ids
        assert exhausted.job_id not in ids

    def test_counts_by_status(self, queue):
        queue.enqueue("ingest_filer", {"cik": 1})
        queue.enqueue("ingest_filer", {"cik": 2})
        queue.claim()
        assert queue.status_counts() == {"pending": 1, "running": 1}


class TestClaimByKey:
    """A stage that knows exactly which unit of work it wants claims it directly,
    rather than taking whatever the queue hands out next."""

    def test_claims_the_named_job(self, queue):
        queue.enqueue("ingest_filer", {"cik": 1})
        wanted = queue.enqueue("ingest_filer", {"cik": 320193})

        claimed = queue.claim_by_key(idempotency_key("ingest_filer", {"cik": 320193}))
        assert claimed.job_id == wanted.job_id
        assert claimed.status == "running"
        assert claimed.attempts == 1

    def test_returns_none_for_an_unknown_key(self, queue):
        assert queue.claim_by_key(idempotency_key("ingest_filer", {"cik": 1})) is None

    def test_returns_none_for_a_job_already_running(self, queue):
        queue.enqueue("ingest_filer", {"cik": 1})
        key = idempotency_key("ingest_filer", {"cik": 1})
        queue.claim_by_key(key)
        assert queue.claim_by_key(key) is None

    def test_returns_none_once_the_attempt_limit_is_spent(self, queue):
        queue.enqueue("ingest_filer", {"cik": 1})
        key = idempotency_key("ingest_filer", {"cik": 1})
        for _ in range(MAX_ATTEMPTS):
            queue.fail(queue.claim_by_key(key), "broken")
        assert queue.claim_by_key(key) is None


class TestReopen:
    """`--force` exists because prompts get retuned and extractors get fixed. It has to
    reset the attempt count too, or a job that failed twice comes back nearly spent."""

    def test_makes_a_completed_job_claimable_again(self, queue):
        job = queue.enqueue("pass_a", {"cik": 1})
        queue.claim()
        queue.finish(job, "{}")

        reopened = queue.reopen(job)
        assert reopened.status == "pending"
        assert reopened.attempts == 0
        assert reopened.finished_at is None
        assert queue.claim() is not None

    def test_clears_the_previous_output_path(self, queue):
        job = queue.enqueue("pass_a", {"cik": 1})
        queue.claim()
        queue.finish(job, "{}")
        assert queue.reopen(job).output_path is None

    def test_revives_a_job_that_had_given_up(self, queue):
        queue.enqueue("ingest_filer", {"cik": 1})
        key = idempotency_key("ingest_filer", {"cik": 1})
        for _ in range(MAX_ATTEMPTS):
            queue.fail(queue.claim_by_key(key), "broken")
        assert queue.claim_by_key(key) is None

        queue.reopen(queue.find(key))
        assert queue.claim_by_key(key) is not None
