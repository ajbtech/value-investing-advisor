"""The CI workflow is a load-bearing part of branch protection.

`main` requires one check, `ci-green`, to pass. That job exists purely so the required
check has a stable name: requiring the matrix jobs directly means listing four names
that change whenever the matrix does, and a required check that no longer exists is not
enforced — it is silently skipped. So the gate is tested like any other invariant.
"""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_workflow_parses(workflow):
    assert workflow["jobs"]


def test_runs_on_pull_requests(workflow):
    """Branch protection can only require a check that actually runs on the PR."""
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert "pull_request" in triggers


def test_tests_both_operating_systems(workflow):
    """The primary dev machine is Windows; users install on both."""
    matrix = workflow["jobs"]["test"]["strategy"]["matrix"]
    assert set(matrix["os"]) == {"ubuntu-latest", "windows-latest"}


class TestTheGateJob:
    def test_exists(self, workflow):
        assert "ci-green" in workflow["jobs"]

    def test_waits_for_the_matrix(self, workflow):
        needs = workflow["jobs"]["ci-green"]["needs"]
        needs = [needs] if isinstance(needs, str) else needs
        assert "test" in needs

    def test_runs_even_when_the_matrix_fails(self, workflow):
        """Without `if: always()` the gate is skipped on failure, and a skipped
        required check can be interpreted as not blocking."""
        assert "always()" in str(workflow["jobs"]["ci-green"]["if"])

    def test_fails_when_any_matrix_job_failed(self, workflow):
        """The whole point: green here must mean green everywhere."""
        steps = str(workflow["jobs"]["ci-green"]["steps"])
        assert "needs.test.result" in steps
        assert "success" in steps
