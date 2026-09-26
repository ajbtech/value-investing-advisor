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


def test_the_token_is_read_only(workflow):
    """The repository is public and CI only reads code. A workflow token that can
    write is one compromised action away from writing to the repository."""
    assert workflow["permissions"] == {"contents": "read"}


def test_installs_exactly_what_the_lockfile_says(workflow):
    """Without --locked, a stale uv.lock is silently re-resolved and CI tests versions
    nobody chose. With it, a stale lock fails the build and says so."""
    steps = workflow["jobs"]["test"]["steps"]
    syncs = [s["run"] for s in steps if "uv sync" in str(s.get("run", ""))]
    assert syncs
    assert all("--locked" in run for run in syncs)


def test_dependencies_and_actions_get_update_prs():
    """Dependabot proposes updates as ordinary PRs, which then have to pass ci-green."""
    config = yaml.safe_load((WORKFLOW.parent.parent / "dependabot.yml").read_text("utf-8"))
    ecosystems = {update["package-ecosystem"] for update in config["updates"]}
    assert {"uv", "github-actions"} <= ecosystems


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
