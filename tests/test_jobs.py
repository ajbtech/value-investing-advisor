"""The job table is the only resume mechanism, so its contract is tested hard.

Four properties matter, and each maps to a rule in CLAUDE.md: a job is identified by
what it would produce (check before spending), output is durable before the row says
so (write then mark), resume is a plain query, and a job that has failed too often
stops being retried.
"""

import json

import pytest

from dossier.jobs import MAX_ATTEMPTS, JobQueue, idempotency_key
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
