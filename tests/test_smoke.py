"""Packaging smoke test for qa-cli.

Pins the console-entry contract: the package imports, exposes a non-empty
version, and `qa_cli.cli:main` is callable. Stdlib-only — no browser, no
network.

Run: uv run --no-project --with pytest pytest tests/ -q
"""
from __future__ import annotations

import importlib
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def test_package_imports_and_has_version() -> None:
    import qa_cli

    assert qa_cli.__version__, "qa_cli.__version__ is empty/missing"


def test_cli_main_is_callable() -> None:
    import qa_cli.cli as cli

    assert callable(cli.main), "qa_cli.cli.main is not callable"


def test_entry_point_resolves() -> None:
    module_name, attr = "qa_cli.cli:main".split(":", 1)
    entry = getattr(importlib.import_module(module_name), attr)
    assert callable(entry), "entry point qa_cli.cli:main is not callable"


def test_version_flag_reads_package_version(capsys) -> None:
    import qa_cli
    from qa_cli.cli import main

    code = main(["--version"])
    assert code == 0
    out = capsys.readouterr().out
    assert qa_cli.__version__ in out
