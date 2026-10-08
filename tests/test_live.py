"""Live Figma API checks. Skipped unless FIGMA_TOKEN and FIGMA_TEST_FILE_KEY are set.

FIGMA_TEST_NODE_ID (optional) names a frame to export. The comment test posts one
comment and deletes it again.
"""

import io
import json
import os
import stat
import sys
import time

import pytest

from figma_cli.cli import main

TOKEN = os.environ.get("FIGMA_TOKEN", "").strip()
FILE_KEY = os.environ.get("FIGMA_TEST_FILE_KEY", "").strip()
NODE_ID = os.environ.get("FIGMA_TEST_NODE_ID", "").strip()

pytestmark = pytest.mark.skipif(
    not (TOKEN and FILE_KEY), reason="FIGMA_TOKEN and FIGMA_TEST_FILE_KEY not set"
)


def _run(capsys, *argv):
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_auth_check(capsys):
    start = time.monotonic()
    code, out = _run(capsys, "auth", "check")
    assert code == 0, out
    assert set(out) == {"id", "handle", "email"}
    assert time.monotonic() - start < 2


def test_bogus_token_is_structured(monkeypatch, capsys):
    monkeypatch.setenv("FIGMA_TOKEN", "bogus")
    code, out = _run(capsys, "auth", "check")
    assert code == 3
    assert out["status"] in (401, 403)


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


def test_file_get_depth_one(capsys):
    code, out = _run(capsys, "file", "get", FILE_KEY, "--depth", "1")
    assert code == 0, out
    pages = out["document"]["children"]
    assert pages
    assert all("children" not in page for page in pages)


@pytest.mark.skipif(not NODE_ID, reason="FIGMA_TEST_NODE_ID not set")
def test_export_png(tmp_path, capsys):
    argv = ("export", FILE_KEY, "--nodes", NODE_ID, "--output", str(tmp_path))
    code, out = _run(capsys, *argv)
    assert code == 0, out
    with open(out["files"][0]["path"], "rb") as fh:
        assert fh.read(4) == b"\x89PNG"


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
