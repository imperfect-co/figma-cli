"""Live Figma API checks. Skipped unless FIGMA_TOKEN is set; the file, export,
comment, node and variable checks also need FIGMA_TEST_FILE_KEY.

FIGMA_TEST_NODE_ID (optional) names a frame to export and to fetch with node get.
The comment test posts one comment and deletes it again.

The variables endpoints are Enterprise only, so the variable check accepts either
the collections or a structured 403.

The OAuth check needs FIGMA_OAUTH_REFRESH_TOKEN, FIGMA_CLIENT_ID and
FIGMA_CLIENT_SECRET. Each refresh invalidates the user's previous access token for
that app, so point it at an OAuth app used only for testing.
"""

import io
import json
import os
import stat
import sys
import time

import pytest

from figma_cli import oauth
from figma_cli.cli import main
from figma_cli.client import FigmaClient

TOKEN = os.environ.get("FIGMA_TOKEN", "").strip()
FILE_KEY = os.environ.get("FIGMA_TEST_FILE_KEY", "").strip()
NODE_ID = os.environ.get("FIGMA_TEST_NODE_ID", "").strip()
OAUTH = {
    name: os.environ.get(name, "").strip()
    for name in ("FIGMA_OAUTH_REFRESH_TOKEN", "FIGMA_CLIENT_ID", "FIGMA_CLIENT_SECRET")
}

needs_token = pytest.mark.skipif(not TOKEN, reason="FIGMA_TOKEN not set")
needs_file = pytest.mark.skipif(
    not (TOKEN and FILE_KEY), reason="FIGMA_TOKEN or FIGMA_TEST_FILE_KEY not set"
)


def _run(capsys, *argv):
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


@needs_token
def test_auth_check(capsys):
    start = time.monotonic()
    code, out = _run(capsys, "auth", "check")
    assert code == 0, out
    assert set(out) == {"id", "handle", "email"}
    assert time.monotonic() - start < 2


@needs_token
def test_bogus_token_is_structured(monkeypatch, capsys):
    monkeypatch.setenv("FIGMA_TOKEN", "bogus")
    code, out = _run(capsys, "auth", "check")
    assert code == 3
    assert out["status"] in (401, 403)


@needs_token
def test_login_then_check_from_token_file(home, monkeypatch, capsys):
    monkeypatch.delenv("FIGMA_TOKEN")
    monkeypatch.setattr(sys, "stdin", io.StringIO(TOKEN + "\n"))
    code, out = _run(capsys, "auth", "login")
    assert code == 0, out
    path = home / ".config" / "figma" / "token"
    assert out["token_path"] == str(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    code, checked = _run(capsys, "auth", "check")
    assert code == 0, checked
    assert checked == {key: out[key] for key in ("id", "handle", "email")}


@needs_file
def test_file_get_depth_one(capsys):
    code, out = _run(capsys, "file", "get", FILE_KEY, "--depth", "1")
    assert code == 0, out
    pages = out["document"]["children"]
    assert pages
    assert all("children" not in page for page in pages)


@needs_file
@pytest.mark.skipif(not NODE_ID, reason="FIGMA_TEST_NODE_ID not set")
def test_export_png(tmp_path, capsys):
    argv = ("export", FILE_KEY, "--nodes", NODE_ID, "--output", str(tmp_path))
    code, out = _run(capsys, *argv)
    assert code == 0, out
    with open(out["files"][0]["path"], "rb") as fh:
        assert fh.read(4) == b"\x89PNG"


@needs_file
@pytest.mark.skipif(not NODE_ID, reason="FIGMA_TEST_NODE_ID not set")
def test_node_get_live(capsys):
    start = time.monotonic()
    code, out = _run(capsys, "node", "get", FILE_KEY, "--nodes", NODE_ID)
    elapsed = time.monotonic() - start
    assert code == 0, out
    assert out["nodes"][NODE_ID]["document"]["id"] == NODE_ID
    assert elapsed < 2
    code, whole = _run(capsys, "file", "get", FILE_KEY)
    assert code == 0, whole
    assert len(json.dumps(out)) < len(json.dumps(whole))


@needs_file
def test_variable_list_live(capsys):
    """Enterprise tokens list collections; any other plan (or scope) gets a 403."""
    code, out = _run(capsys, "variable", "list", FILE_KEY)
    if code == 0:
        assert isinstance(out["meta"]["variableCollections"], dict)
    else:
        assert (code, out["error"], out["status"]) == (3, "forbidden", 403), out


@needs_file
def test_comment_round_trip(capsys):
    code, posted = _run(capsys, "comment", "post", FILE_KEY, "--message", "test")
    assert code == 0, posted
    try:
        code, listed = _run(capsys, "comment", "list", FILE_KEY)
        assert code == 0, listed
        assert posted["id"] in {c["id"] for c in listed["comments"]}
    finally:
        code, deleted = _run(capsys, "comment", "delete", FILE_KEY, posted["id"])
        assert code == 0, deleted


@pytest.mark.skipif(
    not all(OAUTH.values()),
    reason="FIGMA_OAUTH_REFRESH_TOKEN, FIGMA_CLIENT_ID or FIGMA_CLIENT_SECRET not set",
)
def test_oauth_refresh_and_me(home, monkeypatch, capsys):
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    monkeypatch.delenv("FIGMA_API_BASE", raising=False)
    client = (OAUTH["FIGMA_CLIENT_ID"], OAUTH["FIGMA_CLIENT_SECRET"])
    path = oauth.save_oauth_token(
        {
            "access_token": "expired",
            "refresh_token": OAUTH["FIGMA_OAUTH_REFRESH_TOKEN"],
            "expires_at": 0,
            "client_id": client[0],
            "client_secret": client[1],
        }
    )
    code, out = _run(capsys, "auth", "check")
    assert code == 0, out
    assert set(out) == {"id", "handle", "email"}
    stored = json.loads(path.read_text())
    assert stored["access_token"] != "expired"
    assert stored["expires_at"] > time.time()
    bearer = FigmaClient(stored["access_token"], bearer=True)
    assert bearer.me()["id"] == out["id"]
