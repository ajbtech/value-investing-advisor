"""The prepare/load commands' shared handling of files and usage errors.

Review finding 5: `analyze`, `value` and `thesis` each wrote `--out`, read the loaded
JSON back and reported the same errors by hand. None of that was tested, so these pin
the behaviour down before it moves into one place.
"""

import json

import pytest

from dossier.cli import main

VALID_UA = "Jane Doe jane@example.com"
COMMANDS = [
    ["analyze", "--cik", "1"],
    ["value", "--cik", "1"],
    ["thesis", "--cik", "1"],
]


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DOSSIER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EDGAR_USER_AGENT", VALID_UA)
    return tmp_path


@pytest.mark.parametrize("command", COMMANDS)
def test_neither_prepare_nor_load_is_a_usage_error(data_dir, capsys, command):
    assert main([*command, "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"dossier {command[0]}:" in captured.err


@pytest.mark.parametrize("command", COMMANDS)
def test_loading_a_missing_file_is_a_usage_error(data_dir, capsys, command):
    missing = data_dir / "nowhere.json"
    assert main([*command, "--load", str(missing), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no such file" in captured.err


@pytest.mark.parametrize("command", COMMANDS)
def test_loading_invalid_json_is_a_usage_error(data_dir, capsys, command):
    bad = data_dir / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert main([*command, "--load", str(bad), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not valid JSON" in captured.err


@pytest.mark.parametrize("command", COMMANDS)
def test_prepare_for_an_unknown_filer_is_a_usage_error_on_stderr(data_dir, capsys, command):
    assert main([*command, "--prepare", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"dossier {command[0]}:" in captured.err


def test_prepare_with_out_writes_the_file_and_json_still_goes_to_stdout(
    data_dir, capsys, monkeypatch
):
    """With --json the payload is on stdout as well as in the file, so an agent can
    parse the result either way."""
    import dossier.cli as cli

    monkeypatch.setattr(cli, "prepare_valuation", lambda conn, cik, as_of: {"cik": cik})
    out = data_dir / "input.json"
    assert main(["value", "--cik", "7", "--prepare", "--out", str(out), "--json"]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == {"cik": 7}
    assert json.loads(capsys.readouterr().out) == {"cik": 7}


def test_prepare_with_out_and_no_json_says_where_it_wrote(data_dir, capsys, monkeypatch):
    import dossier.cli as cli

    monkeypatch.setattr(cli, "prepare_valuation", lambda conn, cik, as_of: {"cik": cik})
    out = data_dir / "input.json"
    assert main(["value", "--cik", "7", "--prepare", "--out", str(out)]) == 0
    assert str(out) in capsys.readouterr().out
