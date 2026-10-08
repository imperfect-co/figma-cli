"""Hermetic tests for the CLI entrypoint: no network, no installed scripts required."""

import io
import json
import os
import subprocess
import sys
import tomllib
from importlib import import_module
from pathlib import Path

import pytest

from figma_cli import __version__
from figma_cli.cli import main
from figma_cli.client import FigmaError

ROOT = Path(__file__).resolve().parent.parent
ALIASES = ("figma-cli", "figma")


def _scripts() -> dict[str, str]:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["scripts"]


def _resolve(target: str):
    module, _, attr = target.partition(":")
    return getattr(import_module(module), attr)


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


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
    assert (__version__ if flag == "--version" else "usage:") in result.stdout


# Subcommands: a stub client stands in for the network.


class StubClient:
    def __init__(self):
        self.calls = []

    def me(self):
        self.calls.append(("me",))
        return {"id": "1", "handle": "agent", "email": "a@example.com", "img_url": "x"}

    def get_file(self, file_key, depth=None):
        self.calls.append(("get_file", file_key, depth))
        child = {"id": "0:1", "name": "Page 1", "type": "CANVAS"}
        doc = {"id": "0:0", "name": "Document", "type": "DOCUMENT", "children": [child]}
        return {"name": "Spec", "lastModified": "2026-01-01", "document": doc}

    def list_comments(self, file_key):
        self.calls.append(("list_comments", file_key))
        return {
            "comments": [
                {"id": "7", "message": "hello", "user": {"handle": "agent"}},
                {"id": "8", "message": "reply", "parent_id": "7"},
            ]
        }

    def post_comment(self, file_key, message, comment_id=None, node_id=None):
        self.calls.append(("post_comment", file_key, message, comment_id, node_id))
        return {"id": "9", "message": message}

    def delete_comment(self, file_key, comment_id):
        self.calls.append(("delete_comment", file_key, comment_id))
        return {}


@pytest.fixture
def stub(monkeypatch):
    client = StubClient()
    monkeypatch.setattr("figma_cli.cli.FigmaClient.from_env", lambda: client)
    return client


def test_auth_check_json(stub, capsys):
    assert main(["auth", "check", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "id": "1",
        "handle": "agent",
        "email": "a@example.com",
    }


def test_auth_check_human(stub, capsys):
    assert main(["auth", "check"]) == 0
    assert capsys.readouterr().out == "Authenticated as agent <a@example.com> (1)\n"


def test_file_get_maps_depth(stub, capsys):
    assert main(["file", "get", "KEY", "--depth", "1", "--json"]) == 0
    assert stub.calls == [("get_file", "KEY", 1)]
    assert json.loads(capsys.readouterr().out)["document"]["children"][0]["id"] == "0:1"


def test_file_get_human_tree(stub, capsys):
    assert main(["file", "get", "KEY"]) == 0
    assert stub.calls == [("get_file", "KEY", None)]
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "Spec (last modified 2026-01-01)",
        "DOCUMENT Document (0:0)",
        "  CANVAS Page 1 (0:1)",
    ]


@pytest.mark.parametrize("depth", ["0", "-1", "two"])
def test_file_get_rejects_bad_depth(depth, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["file", "get", "KEY", "--depth", depth])
    assert exc.value.code == 2


def test_comment_list(stub, capsys):
    assert main(["comment", "list", "KEY"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("7") and lines[0].endswith("agent: hello")
    assert "(reply to 7)" in lines[1]
    assert main(["comment", "list", "KEY", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["comments"]) == 2


def test_comment_post_flags(stub, capsys):
    argv = ["comment", "post", "KEY", "--message", "hi", "--comment-id", "7"]
    assert main([*argv, "--node-id", "1:2", "--json"]) == 0
    assert stub.calls == [("post_comment", "KEY", "hi", "7", "1:2")]
    assert json.loads(capsys.readouterr().out) == {"id": "9", "message": "hi"}


def test_comment_post_requires_message(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["comment", "post", "KEY"])
    assert exc.value.code == 2


def test_comment_delete(stub, capsys):
    assert main(["comment", "delete", "KEY", "7", "--json"]) == 0
    assert stub.calls == [("delete_comment", "KEY", "7")]
    assert json.loads(capsys.readouterr().out) == {"deleted": True, "id": "7"}
    assert main(["comment", "delete", "KEY", "7"]) == 0
    assert capsys.readouterr().out == "Deleted comment 7\n"


@pytest.mark.parametrize(
    "argv",
    [
        ["auth"],
        ["file", "get"],
        ["export", "KEY"],
        ["export", "KEY", "--nodes", "1:2", "--format", "gif"],
        ["export", "KEY", "--nodes", " , "],
        ["comment", "delete", "KEY"],
    ],
)
def test_usage_errors_exit_two(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2


def test_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    for name in ("auth", "file", "export", "comment"):
        assert name in out


def _raise(payload):
    def fail(*args, **kwargs):
        raise FigmaError(payload)

    return fail


def test_error_json_on_stdout(stub, monkeypatch, capsys):
    payload = {"error": "rate_limit_exceeded", "status": 429, "retry_after": 30}
    monkeypatch.setattr(stub, "me", _raise(payload))
    assert main(["auth", "check", "--json"]) == 3
    assert json.loads(capsys.readouterr().out) == payload


def test_error_human_on_stderr(stub, monkeypatch, capsys):
    payload = {"error": "forbidden", "status": 403, "message": "Invalid token"}
    monkeypatch.setattr(stub, "me", _raise(payload))
    assert main(["auth", "check"]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "forbidden (HTTP 403): Invalid token" in captured.err


def test_missing_token_exits_three(home, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    assert main(["auth", "check", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["error"] == "missing_token"


# auth login: token acquisition, with a stub standing in for GET /v1/me.


class FakeStdin(io.StringIO):
    def __init__(self, text: str = "", tty: bool = False):
        super().__init__(text)
        self.tty = tty

    def isatty(self):
        return self.tty


@pytest.fixture
def login_stub(monkeypatch):
    """Record the token login validates with; fail if it goes through from_env."""
    seen = []

    def build(token, base):
        seen.append((token, base))
        return StubClient()

    def no_env():
        raise AssertionError("auth login must not call FigmaClient.from_env")

    monkeypatch.setattr("figma_cli.login.FigmaClient", build)
    monkeypatch.setattr("figma_cli.cli.FigmaClient.from_env", no_env)
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    monkeypatch.delenv("FIGMA_API_BASE", raising=False)
    return seen


def _stored(home: Path) -> str:
    return (home / ".config" / "figma" / "token").read_text()


def test_login_token_flag(login_stub, home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", FakeStdin("ignored"))
    assert main(["auth", "login", "--token", " figd_flag ", "--json"]) == 0
    assert login_stub == [("figd_flag", "https://api.figma.com")]
    assert _stored(home) == "figd_flag\n"
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "id": "1",
        "handle": "agent",
        "email": "a@example.com",
        "token_path": str(home / ".config" / "figma" / "token"),
    }


@pytest.mark.parametrize("argv", [["--token", "-"], []])
def test_login_reads_stdin(argv, login_stub, home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", FakeStdin("figd_piped\n"))
    assert main(["auth", "login", *argv]) == 0
    assert login_stub[0][0] == "figd_piped"
    assert _stored(home) == "figd_piped\n"
    out = capsys.readouterr().out
    assert "Authenticated as agent <a@example.com> (1)" in out
    assert "Token saved to" in out


@pytest.mark.parametrize("argv", [["--token", "-"], [], ["--token", " "]])
def test_login_empty_token_exits_two(argv, login_stub, home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", FakeStdin("  \n"))
    assert main(["auth", "login", *argv, "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "empty_token"
    assert login_stub == []
    assert not (home / ".config").exists()


@pytest.mark.parametrize("flag, opened", [([], True), (["--no-browser"], False)])
def test_login_interactive_prompt(flag, opened, login_stub, home, monkeypatch, capsys):
    urls = []
    monkeypatch.setattr(sys, "stdin", FakeStdin(tty=True))
    monkeypatch.setattr("getpass.getpass", lambda prompt: "figd_typed")
    monkeypatch.setattr("webbrowser.open", urls.append)
    assert main(["auth", "login", *flag]) == 0
    assert urls == (["https://www.figma.com/settings"] if opened else [])
    err = capsys.readouterr().err
    for scope in (
        "current_user:read",
        "file_content:read",
        "file_comments:read",
        "file_comments:write",
    ):
        assert scope in err
    assert _stored(home) == "figd_typed\n"


def test_login_rejected_token_writes_nothing(login_stub, home, monkeypatch, capsys):
    payload = {"error": "forbidden", "status": 403, "message": "Invalid token"}
    monkeypatch.setattr(StubClient, "me", _raise(payload))
    monkeypatch.setattr(sys, "stdin", FakeStdin("figd_bad"))
    assert main(["auth", "login", "--json"]) == 3
    assert json.loads(capsys.readouterr().out) == payload
    assert not (home / ".config").exists()


@pytest.mark.parametrize(
    "token", ["figd_a\nfigd_b", "figd_a b", "figd_\x00", "figd_\u20ac"]
)
def test_login_malformed_token_exits_two(token, login_stub, home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", FakeStdin(token))
    assert main(["auth", "login", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_token"
    assert login_stub == []
    assert not (home / ".config").exists()


@pytest.mark.parametrize("argv", [["--token", "figd_new"], []])
def test_login_pat_removes_oauth_tokens(argv, login_stub, home, monkeypatch, capsys):
    """token.json outranks the PAT file, so saving a PAT must remove it."""
    oauth_file = home / ".config" / "figma" / "token.json"
    oauth_file.parent.mkdir(parents=True)
    oauth_file.write_text('{"access_token": "figu_stale"}')
    monkeypatch.setattr(sys, "stdin", FakeStdin("figd_new\n"))
    assert main(["auth", "login", *argv]) == 0
    assert not oauth_file.exists()
    assert _stored(home) == "figd_new\n"


def test_login_rejected_pat_keeps_oauth_tokens(login_stub, home, monkeypatch, capsys):
    oauth_file = home / ".config" / "figma" / "token.json"
    oauth_file.parent.mkdir(parents=True)
    oauth_file.write_text('{"access_token": "figu_kept"}')
    payload = {"error": "forbidden", "status": 403, "message": "Invalid token"}
    monkeypatch.setattr(StubClient, "me", _raise(payload))
    monkeypatch.setattr(sys, "stdin", FakeStdin("figd_bad"))
    assert main(["auth", "login", "--json"]) == 3
    assert oauth_file.read_text() == '{"access_token": "figu_kept"}'
