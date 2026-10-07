"""Transport tests: mocked status handling, plus real localhost sockets."""

import email.message
import email.utils
import io
import json
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


def test_missing_token(monkeypatch):
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
        path = tmp_path / "1-2.png"
        assert out == {"files": [{"node_id": "1:2", "path": str(path), "bytes": 24}]}
        assert path.read_bytes().startswith(b"\x89PNG")
        assert "ids=1%3A2" in api_rec.requests[0][1]
        assert _has_token(api_rec.requests[0][2])
        assert [r[1] for r in cdn_rec.requests] == ["/hop/1-2.png", "/1-2.png"]
        assert not any(_has_token(r[2]) for r in cdn_rec.requests)
        assert not any(TOKEN in json.dumps(r) for r in cdn_rec.requests)
