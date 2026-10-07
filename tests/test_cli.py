"""Hermetic tests for the CLI entrypoint: no network, no installed scripts required."""

import os
import subprocess
import sys
import tomllib
from importlib import import_module
from pathlib import Path

import pytest

from figma_cli import __version__
from figma_cli.cli import main

ROOT = Path(__file__).resolve().parent.parent
ALIASES = ("figma-cli", "figma")


def _scripts() -> dict[str, str]:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["scripts"]


def _resolve(target: str):
    module, _, attr = target.partition(":")
    return getattr(import_module(module), attr)


def test_version_is_0_1_0():
    assert __version__ == "0.1.0"


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "0.1.0" in capsys.readouterr().out


def test_help_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage:" in out
    assert "--version" in out


def test_no_args_exits_zero():
    assert main([]) == 0


def test_unknown_flag_exits_two(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--no-such-flag"])
    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_both_aliases_declared():
    assert {name: _scripts().get(name) for name in ALIASES} == {
        name: "figma_cli.cli:main" for name in ALIASES
    }


@pytest.mark.parametrize("alias", ALIASES)
def test_alias_reports_its_own_name(alias, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [alias, "--version"])
    with pytest.raises(SystemExit) as exc:
        _resolve(_scripts()[alias])()
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"{alias} {__version__}"


@pytest.mark.parametrize("flag", ["--version", "--help"])
def test_module_execution(flag):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "figma_cli.cli", flag],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert ("0.1.0" if flag == "--version" else "usage:") in result.stdout
