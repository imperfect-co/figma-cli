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


VARIABLES = {
    "status": 200,
    "error": False,
    "meta": {
        "variableCollections": {
            "VariableCollectionId:1:1": {
                "id": "VariableCollectionId:1:1",
                "name": "Colors",
                "modes": [
                    {"modeId": "1:0", "name": "Light"},
                    {"modeId": "1:1", "name": "Dark"},
                ],
            }
        },
        "variables": {
            "VariableID:1:2": {
                "id": "VariableID:1:2",
                "name": "brand/primary",
                "variableCollectionId": "VariableCollectionId:1:1",
                "resolvedType": "COLOR",
                "valuesByMode": {
                    "1:0": {"r": 1, "g": 0, "b": 0, "a": 1},
                    "1:1": {"r": 0, "g": 0, "b": 0, "a": 0.5},
                },
            },
            "VariableID:1:3": {
                "id": "VariableID:1:3",
                "name": "text/default",
                "variableCollectionId": "VariableCollectionId:1:1",
                "resolvedType": "COLOR",
                "valuesByMode": {
                    "1:0": {"type": "VARIABLE_ALIAS", "id": "VariableID:1:2"},
                    "1:1": {"type": "VARIABLE_ALIAS", "id": "VariableID:9:9"},
                },
            },
            "VariableID:1:4": {
                "id": "VariableID:1:4",
                "name": "radius",
                "variableCollectionId": "VariableCollectionId:1:1",
                "resolvedType": "FLOAT",
                "valuesByMode": {"1:0": 4, "1:1": 8},
            },
        },
    },
}


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

    def get_nodes(self, file_key, node_ids, depth=None, geometry=None):
        self.calls.append(("get_nodes", file_key, node_ids, depth, geometry))
        frame = {"id": "1:2", "name": "Card", "type": "FRAME"}
        frame["children"] = [{"id": "1:3", "name": "Title", "type": "TEXT"}]
        return {
            "name": "Spec",
            "lastModified": "2026-01-01",
            "nodes": {"1:2": {"document": frame}, "9:9": None},
        }

    def get_variables(self, file_key, published=False):
        self.calls.append(("get_variables", file_key, published))
        return VARIABLES

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


def test_node_get_maps_flags(stub, capsys):
    argv = ["node", "get", "KEY", "--nodes", "1:2, 9:9", "--depth", "2"]
    assert main([*argv, "--geometry", "paths", "--json"]) == 0
    assert stub.calls == [("get_nodes", "KEY", ["1:2", "9:9"], 2, "paths")]
    out = json.loads(capsys.readouterr().out)
    assert out["nodes"]["1:2"]["document"]["id"] == "1:2"
    assert out["nodes"]["9:9"] is None


def test_node_get_human_reports_missing_node(stub, capsys):
    assert main(["node", "get", "KEY", "--nodes", "1:2,9:9"]) == 0
    assert stub.calls == [("get_nodes", "KEY", ["1:2", "9:9"], None, None)]
    assert capsys.readouterr().out.splitlines() == [
        "Spec (last modified 2026-01-01)",
        "FRAME Card (1:2)",
        "  TEXT Title (1:3)",
        "Node 9:9: not found",
    ]


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["--nodes", ""],
        ["--nodes", " , "],
        ["--nodes", "1:2", "--depth", "0"],
        ["--nodes", "1:2", "--depth", "two"],
        ["--nodes", "1:2", "--geometry", "bounds"],
    ],
)
def test_node_get_usage_errors(extra, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["node", "get", "KEY", *extra])
    assert exc.value.code == 2


@pytest.mark.parametrize("published", [False, True])
def test_variable_list_json(published, stub, capsys):
    argv = ["variable", "list", "KEY", "--json"]
    assert main(argv + ["--published"] * published) == 0
    assert stub.calls == [("get_variables", "KEY", published)]
    assert json.loads(capsys.readouterr().out) == VARIABLES


def test_variable_list_human(stub, capsys):
    assert main(["variable", "list", "KEY"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Colors (VariableCollectionId:1:1) [modes: Light, Dark]",
        "  brand/primary  COLOR  Light=#FF0000  Dark=#00000080",
        "  text/default  COLOR  Light=-> brand/primary  Dark=-> VariableID:9:9",
        "  radius  FLOAT  Light=4  Dark=8",
    ]


def test_variable_list_human_published_shape(stub, monkeypatch, capsys):
    published = {
        "meta": {
            "variableCollections": {"C:1": {"id": "C:1", "name": "Tokens"}},
            "variables": {
                "V:1": {
                    "id": "V:1",
                    "name": "gap",
                    "variableCollectionId": "C:1",
                    "resolvedDataType": "FLOAT",
                },
            },
        }
    }
    monkeypatch.setattr(stub, "get_variables", lambda *a: published)
    assert main(["variable", "list", "KEY", "--published"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Tokens (C:1)", "  gap  FLOAT"]


def test_variable_list_human_extended_collection(stub, monkeypatch, capsys):
    blue = {"r": 0, "g": 0, "b": 1, "a": 1}
    extended = {
        "meta": {
            "variableCollections": {
                "C:1": {
                    "name": "Brand",
                    "modes": [{"modeId": "1:0", "name": "Light"}],
                    "variableIds": ["V:1", "V:gone"],
                },
                "C:2": {
                    "name": "Brand B",
                    "isExtension": True,
                    "modes": [
                        {"modeId": "2:0", "name": "B", "parentModeId": "1:0"},
                        {"modeId": "2:1", "name": "B2", "parentModeId": "1:0"},
                        {"modeId": "2:2", "name": "Unset"},
                    ],
                    "variableIds": [],
                    "inheritedVariableIds": ["V:1"],
                    "variableOverrides": {"V:1": {"2:0": blue}},
                },
            },
            "variables": {
                "V:1": {
                    "id": "V:1",
                    "name": "brand/primary",
                    "variableCollectionId": "C:1",
                    "resolvedType": "COLOR",
                    "valuesByMode": {"1:0": {"r": 1, "g": 0, "b": 0, "a": 1}},
                },
            },
        }
    }
    monkeypatch.setattr(stub, "get_variables", lambda *a: extended)
    assert main(["variable", "list", "KEY"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Brand (C:1) [modes: Light]",
        "  brand/primary  COLOR  Light=#FF0000",
        "Brand B (C:2) [modes: B, B2, Unset]",
        "  brand/primary  COLOR  B=#0000FF  B2=#FF0000",
    ]


def test_variable_list_survives_mode_cycle(stub, monkeypatch, capsys):
    looped = {
        "meta": {
            "variableCollections": {
                "C:1": {
                    "name": "Loop",
                    "modes": [{"modeId": "1:0", "name": "A", "parentModeId": "1:0"}],
                    "variableIds": ["V:1"],
                }
            },
            "variables": {"V:1": {"name": "gap", "resolvedType": "FLOAT"}},
        }
    }
    monkeypatch.setattr(stub, "get_variables", lambda *a: looped)
    assert main(["variable", "list", "KEY"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Loop (C:1) [modes: A]",
        "  gap  FLOAT",
    ]


def test_variable_list_empty(stub, monkeypatch, capsys):
    monkeypatch.setattr(stub, "get_variables", lambda *a: {"meta": {}})
    assert main(["variable", "list", "KEY"]) == 0
    assert capsys.readouterr().out == "No variables.\n"


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
    for name in ("auth", "file", "node", "variable", "export", "comment"):
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


def test_export_unrendered_node_raises_render_failed(
    stub, monkeypatch, tmp_path, capsys
):
    images = {"1:2": "https://example.com/a.png", "3:4": None, "5:6": ""}
    monkeypatch.setattr(stub, "get_images", lambda *a: images, raising=False)
    monkeypatch.setattr("figma_cli.cli.download", _raise({"error": "unexpected"}))
    out_dir = tmp_path / "out"
    argv = ["export", "KEY", "--nodes", "1:2,3:4,5:6", "--output", str(out_dir)]
    assert main([*argv, "--json"]) == 3
    assert json.loads(capsys.readouterr().out) == {
        "error": "render_failed",
        "message": "no image URL returned",
        "nodes": ["3:4", "5:6"],
    }
    assert not out_dir.exists()


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
    for needle in (
        "FIGMA_CLIENT_ID",
        "FIGMA_CLIENT_SECRET",
        "https://www.figma.com/developers/apps",
        "http://127.0.0.1:54321/callback",
        "current_user:read",
        "file_content:read",
        "file_comments:read",
        "file_comments:write",
    ):
        assert needle in err
    assert _stored(home) == "figd_typed\n"


@pytest.fixture
def oauth_stub(monkeypatch):
    """Record the client pair the browser flow would start with."""
    seen = []

    def fake_login(client, port, open_browser):
        seen.append(client)
        return {"id": "1"}, Path("token.json")

    monkeypatch.setattr("figma_cli.oauth.login", fake_login)
    monkeypatch.delenv("FIGMA_CLIENT_ID", raising=False)
    monkeypatch.delenv("FIGMA_CLIENT_SECRET", raising=False)
    return seen


def _app_json(home: Path) -> Path:
    return home / ".config" / "figma" / "app.json"


def test_login_uses_stored_app(oauth_stub, login_stub, home, monkeypatch):
    path = _app_json(home)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"client_id": "cid", "client_secret": "sec"}))
    monkeypatch.setattr(sys, "stdin", FakeStdin(tty=True))
    monkeypatch.setattr("getpass.getpass", _raise({"error": "prompted"}))
    assert main(["auth", "login", "--json"]) == 0
    assert oauth_stub == [("cid", "sec")]
    assert login_stub == []


def test_login_prompt_saves_app_and_runs_oauth(
    oauth_stub, login_stub, home, monkeypatch, capsys
):
    monkeypatch.setattr(sys, "stdin", FakeStdin(" typed-id \n", tty=True))
    monkeypatch.setattr("getpass.getpass", lambda prompt: " typed-secret ")
    assert main(["auth", "login", "--json"]) == 0
    assert oauth_stub == [("typed-id", "typed-secret")]
    assert login_stub == []
    path = _app_json(home)
    assert json.loads(path.read_text()) == {
        "client_id": "typed-id",
        "client_secret": "typed-secret",
    }
    assert os.stat(path).st_mode & 0o777 == 0o600
    err = capsys.readouterr().err
    assert "http://127.0.0.1:54321/callback" in err
    assert str(path) in err


def test_login_prompt_failed_oauth_saves_nothing(oauth_stub, home, monkeypatch):
    monkeypatch.setattr(sys, "stdin", FakeStdin("typed-id\n", tty=True))
    monkeypatch.setattr("getpass.getpass", lambda prompt: "wrong-secret")
    payload = {"error": "oauth_failed", "message": "invalid_client"}
    monkeypatch.setattr("figma_cli.oauth.login", _raise(payload))
    assert main(["auth", "login", "--json"]) == 3
    assert not _app_json(home).exists()


@pytest.mark.parametrize("typed_id, secret", [("", "unused"), ("typed-id", " ")])
def test_login_skipped_prompt_falls_back_to_pat(
    typed_id, secret, oauth_stub, login_stub, home, monkeypatch
):
    answers = iter([secret, "figd_typed"] if typed_id else ["figd_typed"])
    monkeypatch.setattr(sys, "stdin", FakeStdin(typed_id + "\n", tty=True))
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(answers))
    monkeypatch.setattr("webbrowser.open", lambda url: None)
    assert main(["auth", "login", "--json"]) == 0
    assert oauth_stub == []
    assert login_stub[0][0] == "figd_typed"
    assert _stored(home) == "figd_typed\n"
    assert not _app_json(home).exists()


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
