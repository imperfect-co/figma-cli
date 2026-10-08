"""Transport tests: mocked status handling, plus real localhost sockets."""

import email.message
import email.utils
import io
import json
import os
import stat
import sys
import threading
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from figma_cli.cli import main
from figma_cli.client import (
    TOKEN_HEADER,
    FigmaClient,
    FigmaError,
    download,
    parse_retry_after,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
TOKEN = "figd_test-token-value"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeOpener:
    """Stands in for the urllib opener: records requests, replays one outcome."""

    def __init__(self, status=200, body=b"{}", headers=None):
        self.status, self.body, self.headers = status, body, headers or {}
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        if self.status >= 400:
            hdrs = email.message.Message()
            for key, value in self.headers.items():
                hdrs[key] = value
            raise urllib.error.HTTPError(
                req.full_url, self.status, "err", hdrs, io.BytesIO(self.body)
            )
        return FakeResponse(self.body)


def _client(opener: FakeOpener) -> FigmaClient:
    client = FigmaClient(TOKEN, "https://figma.invalid")
    client._opener = opener
    return client


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (
            401,
            b'{"status":401,"err":"Unauthorized"}',
            {"error": "unauthorized", "status": 401, "message": "Unauthorized"},
        ),
        (
            403,
            b'{"status":403,"err":"Invalid token"}',
            {"error": "forbidden", "status": 403, "message": "Invalid token"},
        ),
        (
            404,
            b'{"status":404,"err":"Not found"}',
            {"error": "not_found", "status": 404, "message": "Not found"},
        ),
        (500, b"<html>oops</html>", {"error": "server_error", "status": 500}),
    ],
)
def test_http_errors_are_structured(status, body, expected):
    with pytest.raises(FigmaError) as exc:
        _client(FakeOpener(status, body)).me()
    assert exc.value.payload == expected
    assert exc.value.exit_code == 3


def test_429_reports_retry_after_without_retrying():
    opener = FakeOpener(429, b"{}", {"Retry-After": "37"})
    with pytest.raises(FigmaError) as exc:
        _client(opener).me()
    assert exc.value.payload == {
        "error": "rate_limit_exceeded",
        "status": 429,
        "retry_after": 37,
    }
    assert len(opener.requests) == 1


def test_parse_retry_after_forms():
    assert parse_retry_after("12") == 12
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    date = email.utils.formatdate(1_000_060, usegmt=True)
    assert parse_retry_after(date, now=1_000_000) == 60
    assert parse_retry_after(date, now=2_000_000) == 0


def test_request_sends_token_and_query():
    opener = FakeOpener(body=b'{"name":"f"}')
    _client(opener).get_file("abc/../x", depth=1)
    req = opener.requests[0]
    assert req.full_url == "https://figma.invalid/v1/files/abc%2F..%2Fx?depth=1"
    assert req.get_header(TOKEN_HEADER.capitalize()) == TOKEN


def test_post_comment_body():
    opener = FakeOpener(body=b'{"id":"9"}')
    _client(opener).post_comment("k", "hi", comment_id="1", node_id="2:3")
    req = opener.requests[0]
    assert req.get_method() == "POST"
    assert json.loads(req.data) == {
        "message": "hi",
        "comment_id": "1",
        "client_meta": {"node_id": "2:3", "node_offset": {"x": 0, "y": 0}},
    }


def test_delete_comment_accepts_empty_body():
    opener = FakeOpener(body=b"")
    assert _client(opener).delete_comment("k", "42") == {}
    assert opener.requests[0].get_method() == "DELETE"
    assert opener.requests[0].full_url.endswith("/v1/files/k/comments/42")


def test_missing_token(home, monkeypatch):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    with pytest.raises(FigmaError) as exc:
        FigmaClient.from_env()
    assert exc.value.payload["error"] == "missing_token"


def test_api_base_from_env(monkeypatch):
    monkeypatch.setenv("FIGMA_TOKEN", TOKEN)
    monkeypatch.setenv("FIGMA_API_BASE", "http://127.0.0.1:9/")
    assert FigmaClient.from_env().base_url == "http://127.0.0.1:9"
    monkeypatch.delenv("FIGMA_API_BASE")
    assert FigmaClient.from_env().base_url == "https://api.figma.com"


def test_download_rejects_non_http_scheme():
    with pytest.raises(FigmaError) as exc:
        download("file:///etc/passwd")
    assert exc.value.payload["error"] == "invalid_asset_url"


# Real localhost sockets below: no mocks between the client and the wire.


class Recorder:
    def __init__(self):
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.routes: dict[str, tuple[int, dict[str, str], bytes]] = {}


@contextmanager
def serve() -> Iterator[tuple[str, Recorder]]:
    rec = Recorder()

    class Handler(BaseHTTPRequestHandler):
        def _answer(self):
            rec.requests.append((self.command, self.path, dict(self.headers)))
            status, headers, body = rec.routes.get(
                self.path.split("?")[0], (404, {}, b"{}")
            )
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_DELETE = _answer

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", rec
    finally:
        server.shutdown()
        server.server_close()


def _has_token(headers: dict[str, str]) -> bool:
    return any(k.lower() == TOKEN_HEADER.lower() for k in headers)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_token_request_refuses_redirect(status, monkeypatch, capsys):
    with serve() as (api, api_rec), serve() as (other, other_rec):
        api_rec.routes["/v1/me"] = (status, {"Location": f"{other}/v1/me"}, b"")
        monkeypatch.setenv("FIGMA_API_BASE", api)
        monkeypatch.setenv("FIGMA_TOKEN", TOKEN)

        code = main(["auth", "check", "--json"])

        assert code == 3
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "redirect_refused"
        assert out["status"] == status
        assert len(api_rec.requests) == 1
        assert _has_token(api_rec.requests[0][2])
        assert other_rec.requests == []


def test_export_downloads_asset_without_token(monkeypatch, tmp_path, capsys):
    with serve() as (api, api_rec), serve() as (cdn, cdn_rec):
        images = {"images": {"1:2": f"{cdn}/hop/1-2.png"}, "err": None}
        api_rec.routes["/v1/images/KEY"] = (200, {}, json.dumps(images).encode())
        cdn_rec.routes["/hop/1-2.png"] = (302, {"Location": f"{cdn}/1-2.png"}, b"")
        cdn_rec.routes["/1-2.png"] = (200, {"Content-Type": "image/png"}, PNG)
        monkeypatch.setenv("FIGMA_API_BASE", api)
        monkeypatch.setenv("FIGMA_TOKEN", TOKEN)

        code = main(
            ["export", "KEY", "--nodes", "1:2", "--output", str(tmp_path), "--json"]
        )

        assert code == 0
        out = json.loads(capsys.readouterr().out)
        path = tmp_path / "KEY_1-2.png"
        assert out == {"files": [{"node_id": "1:2", "path": str(path), "bytes": 24}]}
        assert path.read_bytes().startswith(b"\x89PNG")
        assert "ids=1%3A2" in api_rec.requests[0][1]
        assert _has_token(api_rec.requests[0][2])
        assert [r[1] for r in cdn_rec.requests] == ["/hop/1-2.png", "/1-2.png"]
        assert not any(_has_token(r[2]) for r in cdn_rec.requests)
        assert not any(TOKEN in json.dumps(r) for r in cdn_rec.requests)


# Stored token: auth login against a loopback /v1/me, and the from_env fallback.

ME = {"id": "123", "handle": "test", "email": "test@example.com"}


def _token_file(home):
    return home / ".config" / "figma" / "token"


def _seed(home, content: bytes, mode: int = 0o600):
    path = _token_file(home)
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    path.chmod(mode)
    return path


def _login(monkeypatch, api: str, token: str, *argv: str) -> int:
    monkeypatch.setenv("FIGMA_API_BASE", api)
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(token + "\n"))
    return main(["auth", "login", "--json", *argv])


def test_login_first_run_stores_token_0600(home, monkeypatch, capsys):
    with serve() as (api, rec):
        rec.routes["/v1/me"] = (200, {}, json.dumps(ME).encode())
        assert _login(monkeypatch, api, TOKEN) == 0
        assert rec.requests[0][2].get(TOKEN_HEADER) == TOKEN
    path = _token_file(home)
    assert json.loads(capsys.readouterr().out) == {**ME, "token_path": str(path)}
    assert path.read_text() == TOKEN + "\n"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_login_tightens_looser_existing_file(home, monkeypatch, capsys):
    path = _seed(home, b"figd_old\n", mode=0o644)
    with serve() as (api, rec):
        rec.routes["/v1/me"] = (200, {}, json.dumps(ME).encode())
        assert _login(monkeypatch, api, TOKEN) == 0
    assert path.read_text() == TOKEN + "\n"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


@pytest.mark.parametrize("status", [401, 403])
def test_login_rejected_creates_no_file(status, home, monkeypatch, capsys):
    with serve() as (api, rec):
        rec.routes["/v1/me"] = (status, {}, b'{"err": "Invalid token"}')
        assert _login(monkeypatch, api, "figd_bad") == 3
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == status
    assert not (home / ".config").exists()


@pytest.mark.parametrize("status", [401, 403])
def test_login_rejected_preserves_existing_file(status, home, monkeypatch, capsys):
    original = b"figd_working-token\n"
    path = _seed(home, original)
    with serve() as (api, rec):
        rec.routes["/v1/me"] = (status, {}, b"{}")
        assert _login(monkeypatch, api, "figd_bad") == 3
    assert path.read_bytes() == original
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_login_write_failure_is_structured(home, monkeypatch, capsys):
    (home / ".config").write_text("not a directory")
    with serve() as (api, rec):
        rec.routes["/v1/me"] = (200, {}, json.dumps(ME).encode())
        assert _login(monkeypatch, api, TOKEN) == 3
    assert json.loads(capsys.readouterr().out)["error"] == "write_failed"


def test_check_falls_back_to_stripped_token_file(home, monkeypatch, capsys):
    _seed(home, f"  {TOKEN}\n\n".encode())
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    with serve() as (api, rec):
        rec.routes["/v1/me"] = (200, {}, json.dumps(ME).encode())
        monkeypatch.setenv("FIGMA_API_BASE", api)
        assert main(["auth", "check", "--json"]) == 0
        assert rec.requests[0][2].get(TOKEN_HEADER) == TOKEN
    assert json.loads(capsys.readouterr().out) == ME


def test_env_token_overrides_token_file(home, monkeypatch):
    _seed(home, b"figd_from_file\n")
    monkeypatch.setenv("FIGMA_TOKEN", TOKEN)
    assert FigmaClient.from_env().token == TOKEN
    monkeypatch.setenv("FIGMA_TOKEN", " ")
    assert FigmaClient.from_env().token == "figd_from_file"


def test_token_file_directory_counts_as_missing(home, monkeypatch):
    _token_file(home).mkdir(parents=True)
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    with pytest.raises(FigmaError) as exc:
        FigmaClient.from_env()
    assert exc.value.payload["error"] == "missing_token"


def test_undecodable_token_file_is_structured(home, monkeypatch, capsys):
    _seed(home, b"\xff\xfe\x00bad")
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    assert main(["auth", "check", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["error"] == "token_unreadable"


def test_login_replaces_symlink_without_following_it(home, monkeypatch, capsys):
    target = home / "elsewhere"
    target.write_text("untouched\n")
    path = _token_file(home)
    path.parent.mkdir(parents=True)
    path.symlink_to(target)
    with serve() as (api, rec):
        rec.routes["/v1/me"] = (200, {}, json.dumps(ME).encode())
        assert _login(monkeypatch, api, TOKEN) == 0
    assert target.read_text() == "untouched\n"
    assert not path.is_symlink()
    assert path.read_text() == TOKEN + "\n"


def test_login_tightens_existing_directory(home, monkeypatch, capsys):
    _token_file(home).parent.mkdir(parents=True, mode=0o755)
    with serve() as (api, rec):
        rec.routes["/v1/me"] = (200, {}, json.dumps(ME).encode())
        assert _login(monkeypatch, api, TOKEN) == 0
    assert stat.S_IMODE(os.stat(_token_file(home).parent).st_mode) == 0o700


def test_login_failed_write_keeps_old_token(home, monkeypatch, capsys):
    original = b"figd_working-token\n"
    path = _seed(home, original)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("figma_cli.login.os.replace", fail)
    with serve() as (api, rec):
        rec.routes["/v1/me"] = (200, {}, json.dumps(ME).encode())
        assert _login(monkeypatch, api, TOKEN) == 3
    assert json.loads(capsys.readouterr().out)["error"] == "write_failed"
    assert path.read_bytes() == original
    assert sorted(p.name for p in path.parent.iterdir()) == ["token"]


@pytest.mark.parametrize("body", [b"{}", b"[]", b'{"handle": "x"}'])
def test_login_without_identity_keeps_old_token(body, home, monkeypatch, capsys):
    original = b"figd_working-token\n"
    path = _seed(home, original)
    with serve() as (api, rec):
        rec.routes["/v1/me"] = (200, {}, body)
        assert _login(monkeypatch, api, TOKEN) == 3
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_response"
    assert path.read_bytes() == original
