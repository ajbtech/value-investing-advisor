"""Which modules may import which, checked rather than hoped for.

CLAUDE.md says stages communicate only through the store, never by importing each other.
A review found that rule broken in five places — valuation and re-check building the
screener's whole universe, re-check reaching into a private function of the thesis
stage, prices borrowing EDGAR's rate limiter — and nothing had noticed, because nothing
checked. An instruction is not a control; this test is.

The layers:

- **Shared libraries** carry no stage logic and may be imported by anything.
- **Stages** do one step of the pipeline each. A stage may import libraries, and may read
  another stage's *stored output* only through that stage's named accessor — reading
  through the store, with the SQL kept in the module that owns the table.
- **The CLI** wires stages to commands. It may import anything; nothing imports it.
"""

import ast
from pathlib import Path

import dossier

PACKAGE = Path(dossier.__file__).parent

LIBRARIES = {
    "asof",
    "bulk",
    "config",
    "figures",
    "findings",
    "formindex",
    "journal",
    "jobs",
    "models",
    "prompt_files",
    "ratelimit",
    "securities",
    "store",
}

STAGES = {
    "analysis",
    "deregistrations",
    "edgar",
    "extract",
    "ingest",
    "prices",
    "recheck",
    "screens",
    "thesis",
    "universe",
    "valuation",
}

#: The only imports allowed from one stage into another: accessors that read what the
#: other stage stored. Anything added here should be the same kind of thing.
STORED_OUTPUT_READS = {
    ("thesis", "valuation"): {"stored_valuation"},
    ("recheck", "thesis"): {"stored_thesis"},
}


def _imports(module: str) -> list[tuple[str, set[str]]]:
    """(imported dossier module, names imported from it) for every import in a module."""
    tree = ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("dossier"):
            target = node.module.removeprefix("dossier").lstrip(".") or "__init__"
            found.append((target, {alias.name for alias in node.names}))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("dossier."):
                    found.append((alias.name.removeprefix("dossier."), set()))
    return found


def test_every_module_is_classified():
    """A new module has to be placed in a layer, which is a decision worth making."""
    modules = {path.stem for path in PACKAGE.glob("*.py")} - {"__init__", "cli"}
    assert modules == LIBRARIES | STAGES, (
        f"unclassified: {sorted(modules - LIBRARIES - STAGES)}; "
        f"gone: {sorted((LIBRARIES | STAGES) - modules)}"
    )


def test_libraries_import_only_libraries():
    offenders = [
        f"{module} imports {target}"
        for module in sorted(LIBRARIES)
        for target, _ in _imports(module)
        if target not in LIBRARIES and target != "__init__"
    ]
    assert offenders == []


def test_stages_do_not_import_each_other_except_to_read_stored_output():
    offenders = []
    for module in sorted(STAGES):
        for target, names in _imports(module):
            if target not in STAGES:
                continue
            allowed = STORED_OUTPUT_READS.get((module, target), set())
            if not names or not names <= allowed:
                offenders.append(f"{module} imports {sorted(names - allowed)} from {target}")
    assert offenders == []


def test_nothing_imports_the_cli():
    offenders = [
        module
        for module in sorted(LIBRARIES | STAGES)
        if any(target == "cli" for target, _ in _imports(module))
    ]
    assert offenders == []


def test_no_module_imports_a_private_name_from_another():
    """recheck imported thesis._append_journal. A leading underscore is a module saying
    "not part of my interface", and crossing it couples two modules to an internal."""
    offenders = [
        f"{module} imports {name} from {target}"
        for module in sorted(LIBRARIES | STAGES | {"cli"})
        for target, names in _imports(module)
        for name in names
        if name.startswith("_") and not name.startswith("__") and target != module
    ]
    assert offenders == []
