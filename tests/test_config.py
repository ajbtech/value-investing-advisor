"""Where data lives.

Code in the repo; data on the user's machine. The store runs to tens of gigabytes and
is rebuildable from EDGAR, so it has no business inside a checkout.
"""

from pathlib import Path

from dossier.config import Config


class TestDataDirectory:
    def test_defaults_to_the_per_user_data_directory(self, monkeypatch):
        monkeypatch.delenv("DOSSIER_DATA_DIR", raising=False)
        data_dir = Config.load().data_dir
        assert isinstance(data_dir, Path)
        assert "edgar-dossier" in str(data_dir)

    def test_is_overridable_for_tests_and_for_users_with_a_big_disk(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOSSIER_DATA_DIR", str(tmp_path))
        assert Config.load().data_dir == tmp_path

    def test_never_defaults_inside_the_repo(self, monkeypatch):
        monkeypatch.delenv("DOSSIER_DATA_DIR", raising=False)
        repo_root = Path(__file__).resolve().parent.parent
        assert repo_root not in Config.load().data_dir.resolve().parents

    def test_the_store_and_job_output_sit_under_it(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOSSIER_DATA_DIR", str(tmp_path))
        config = Config.load()
        assert config.store_path.parent == tmp_path
        assert config.store_path.suffix == ".sqlite"
        assert tmp_path in config.output_dir.parents or config.output_dir.parent == tmp_path

    def test_the_journal_is_configured_separately(self, monkeypatch, tmp_path):
        """The decision journal is personal and belongs in a directory the user picks,
        never in the tool's public repo."""
        monkeypatch.setenv("DOSSIER_JOURNAL_DIR", str(tmp_path / "journal"))
        assert Config.load().journal_dir == tmp_path / "journal"
