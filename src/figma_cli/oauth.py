"""Figma OAuth 2.0 with PKCE (RFC 7636), for a bring-your-own OAuth app.

Figma authenticates the client on both token calls with HTTP Basic, so the
user's own client id and secret are required; nothing is embedded here. The
redirect URL is fixed at ``http://127.0.0.1:<port>/callback`` because Figma
matches registered redirect URLs exactly, so a busy port is an error rather
than a reason to pick another one.
"""

import base64
import hashlib
import html
import json
import os
import queue
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from figma_cli.client import (
    TIMEOUT_SECONDS,
    FigmaClient,
    FigmaError,
    UsageError,
    _http_error,
    _network_error,
    _RefuseRedirects,
    api_base,
    checked_me,
    token_json_path,
    write_private,
)

AUTHORIZE_URL = "https://www.figma.com/oauth"
TOKEN_PATH = "/v1/oauth/token"
REFRESH_PATH = "/v1/oauth/refresh"
DEFAULT_PORT = 54321
CALLBACK_PATH = "/callback"
CALLBACK_TIMEOUT_SECONDS = 300
POLL_SECONDS = 0.5  # how long a paste can wait for the loopback poll to yield
_LOOPBACK = ("127.0.0.1", "localhost")
PASTE_PROMPT = "Paste the callback URL here (or finish in the browser): "
EXPIRY_MARGIN_SECONDS = 60
SCOPES = (
    "current_user:read",
    "file_content:read",
    "file_comments:read",
    "file_comments:write",
)
_FIELDS = ("access_token", "refresh_token", "expires_at", "client_id", "client_secret")


def generate_code_verifier(length: int = 64) -> str:
    """A high-entropy RFC 7636 code verifier (86 URL-safe characters by default)."""
    return secrets.token_urlsafe(length)


def compute_code_challenge(verifier: str) -> str:
    """The S256 challenge: unpadded base64url of SHA-256(verifier)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def redirect_uri(port: int) -> str:
    return f"http://127.0.0.1:{port}{CALLBACK_PATH}"


def authorization_url(client_id: str, port: int, state: str, challenge: str) -> str:
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(port),
        "scope": " ".join(SCOPES),
        "state": state,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(query)}"


def client_credentials(
    client_id: str | None, client_secret: str | None
) -> tuple[str, str] | None:
    """Resolve the OAuth app from flags, then FIGMA_CLIENT_ID / FIGMA_CLIENT_SECRET.

    Returns None when neither half is configured; a lone half is a usage error.
    """
    cid = (client_id or os.environ.get("FIGMA_CLIENT_ID", "")).strip()
    secret = (client_secret or os.environ.get("FIGMA_CLIENT_SECRET", "")).strip()
    if not cid and not secret:
        return None
    if not (cid and secret):
        missing = "client secret" if cid else "client id"
        message = f"OAuth needs both a client id and a client secret; no {missing}"
        raise UsageError("missing_client_credentials", message)
    return cid, secret


def _token_call(
    path: str, client: tuple[str, str], form: dict[str, str]
) -> dict[str, Any]:
    """POST a form with HTTP Basic client authentication; refuses redirects."""
    basic = base64.b64encode(f"{client[0]}:{client[1]}".encode()).decode("ascii")
    req = urllib.request.Request(
        api_base().rstrip("/") + path,
        data=urllib.parse.urlencode(form).encode(),
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(_RefuseRedirects)
    try:
        with opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        raise _http_error(err) from None
    except OSError as err:
        raise _network_error(err) from None
    except ValueError:
        body = None
    if not isinstance(body, dict) or not body.get("access_token"):
        message = f"{path} returned no access_token"
        raise FigmaError({"error": "invalid_response", "message": message})
    return body


def exchange_code(
    client: tuple[str, str], code: str, verifier: str, port: int
) -> dict[str, Any]:
    form = {
        "redirect_uri": redirect_uri(port),
        "code": code,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }
    return _token_call(TOKEN_PATH, client, form)


def refresh(client: tuple[str, str], refresh_token: str) -> dict[str, Any]:
    return _token_call(REFRESH_PATH, client, {"refresh_token": refresh_token})


def _expires_at(body: dict[str, Any]) -> int:
    try:
        return int(time.time() + float(body.get("expires_in", 0)))
    except (TypeError, ValueError):
        return int(time.time())


def save_oauth_token(record: dict[str, Any]) -> Path:
    path = token_json_path()
    stored = {key: record[key] for key in _FIELDS}
    write_private(path, json.dumps(stored, indent=2) + "\n")
    return path


def _well_formed(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    expires_at = record.get("expires_at")
    if isinstance(expires_at, bool) or not isinstance(expires_at, int | float):
        return False
    strings = (record.get(k) for k in _FIELDS if k != "expires_at")
    return all(isinstance(v, str) and v for v in strings)


def load_oauth_token() -> dict[str, Any]:
    path = token_json_path()
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as err:
        raise FigmaError({"error": "token_unreadable", "message": str(err)}) from None
    if not _well_formed(record):
        message = f"{path} is missing one of {', '.join(_FIELDS)}"
        raise FigmaError({"error": "token_unreadable", "message": message})
    return record


def _expired(record: dict[str, Any]) -> bool:
    return record["expires_at"] <= time.time() + EXPIRY_MARGIN_SECONDS


@contextmanager
def _refresh_lock():
    """Hold an advisory lock on token.json.lock; a no-op where fcntl is absent."""
    try:
        import fcntl  # POSIX only; imported here so Windows can still import us
    except ImportError:
        yield
        return
    path = token_json_path().with_name("token.json.lock")
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as err:
        raise FigmaError({"error": "write_failed", "message": str(err)}) from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def fresh_access_token() -> str:
    """Return a usable access token from token.json, refreshing it if expired.

    Refresh-then-reread under the lock: a sibling process that refreshed first
    leaves an unexpired record, which is used as is, so Figma never sees two
    refreshes racing (each one invalidates the access token before it).
    """
    record = load_oauth_token()
    if not _expired(record):
        return record["access_token"]
    with _refresh_lock():
        record = load_oauth_token()
        if not _expired(record):
            return record["access_token"]
        client = (record["client_id"], record["client_secret"])
        try:
            body = refresh(client, record["refresh_token"])
        except FigmaError as err:
            payload = {**err.payload, "error": "refresh_failed"}
            detail = err.payload.get("message") or err.payload["error"]
            payload["message"] = (
                f"refreshing the OAuth token failed ({detail}); run 'figma auth login'"
            )
            raise FigmaError(payload) from None
        record["access_token"] = body["access_token"]
        record["refresh_token"] = body.get("refresh_token") or record["refresh_token"]
        record["expires_at"] = _expires_at(body)
        save_oauth_token(record)
    return record["access_token"]


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>figma-cli</title></head>
<body><main><h1>{title}</h1><p>{detail}</p></main></body></html>
"""


class OAuthCallbackServer(ThreadingHTTPServer):
    """Loopback server that waits for one valid ``/callback`` request.

    Each connection is served on its own daemon thread, so an idle browser
    preconnect cannot hold the polling loop (and a paste) for the read timeout.
    """

    block_on_close = False  # server_close() must not wait out an idle socket

    def __init__(self, port: int, state: str):
        self.state = state
        self.result: dict[str, str] | None = None
        self._accept_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), _CallbackHandler)

    def accept(self, result: dict[str, str]) -> None:
        """Record ``result`` unless one is already held: the first one wins."""
        with self._accept_lock:
            if self.result is None:
                self.result = result


class _CallbackHandler(BaseHTTPRequestHandler):
    server: OAuthCallbackServer
    timeout = 10  # a browser preconnect that never sends must not stall the flow

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path != CALLBACK_PATH:
            return self._page(404, "Not found", "This is not the OAuth callback.")
        query = dict(urllib.parse.parse_qsl(url.query))
        got = query.get("state", "").encode()
        if not secrets.compare_digest(got, self.server.state.encode()):
            detail = "The state parameter does not match this login attempt."
            return self._page(400, "Bad request", detail)
        if query.get("error") or not query.get("code"):
            self.server.accept({"error": query.get("error") or "missing_code"})
            detail = "Figma did not authorize figma-cli. You can close this tab."
            return self._page(400, "Authorization failed", detail)
        self.server.accept({"code": query["code"]})
        detail = "figma-cli is authorized. You can close this tab."
        self._page(200, "Authorization complete", detail)

    def _page(self, status: int, title: str, detail: str) -> None:
        body = _PAGE.format(title=html.escape(title), detail=html.escape(detail))
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass


def _bind(port: int, state: str) -> OAuthCallbackServer:
    try:
        return OAuthCallbackServer(port, state)
    except OSError as err:
        message = (
            f"cannot listen on 127.0.0.1:{port} ({err.strerror or err}); free the"
            " port or pass --port with another redirect URL registered on your app"
        )
        raise UsageError("port_unavailable", message) from None


def parse_pasted_callback(text: str, state: str, port: int) -> dict[str, str]:
    """Read a pasted callback URL or query string into a callback result.

    Holds a paste to the loopback handler's bar: the path must be the callback
    and ``state`` must match, so a bare code (no state, no CSRF check) is refused.
    Returns ``{"code": ...}`` or ``{"error": ...}``; raises ValueError to re-prompt.
    """
    text = text.strip()
    url = urllib.parse.urlsplit(text)
    if url.scheme or url.netloc:
        try:
            where = (url.scheme, url.hostname, url.port, url.path)
        except ValueError:
            where = None
        if where not in {("http", h, port, CALLBACK_PATH) for h in _LOOPBACK}:
            raise ValueError(f"expected a URL starting with {redirect_uri(port)}")
        text = url.query
    query = dict(urllib.parse.parse_qsl(text.removeprefix("?")))
    if "state" not in query:
        raise ValueError("no state parameter; paste the whole callback URL")
    if not secrets.compare_digest(query["state"].encode(), state.encode()):
        raise ValueError("the state parameter does not match this login attempt")
    if query.get("error"):
        return {"error": query["error"]}
    if not query.get("code"):
        raise ValueError("no code parameter; paste the whole callback URL")
    return {"code": query["code"]}


def _read_pastes(stream, pastes: queue.SimpleQueue) -> None:
    """Queue each non-blank line from ``stream``; return on EOF or a read error."""
    try:
        for line in iter(stream.readline, ""):
            if line.strip():
                pastes.put(line)
    except (OSError, ValueError):
        pass


def _start_paste_reader(stream) -> queue.SimpleQueue:
    pastes: queue.SimpleQueue = queue.SimpleQueue()
    reader = threading.Thread(target=_read_pastes, args=(stream, pastes), daemon=True)
    reader.start()
    return pastes


def _take_paste(server: OAuthCallbackServer, pastes: queue.SimpleQueue) -> None:
    """Move queued pastes into ``server.result``; re-prompt on a rejected one."""
    while server.result is None:
        try:
            line = pastes.get_nowait()
        except queue.Empty:
            return
        try:
            port = server.server_port
            server.accept(parse_pasted_callback(line, server.state, port))
        except ValueError as err:
            print(f"Not accepted: {err}.", file=sys.stderr)
            print(PASTE_PROMPT, end="", file=sys.stderr, flush=True)


def _await_code(
    server: OAuthCallbackServer,
    timeout: float,
    pastes: queue.SimpleQueue | None = None,
) -> str:
    """Wait for the browser redirect or, when ``pastes`` is given, a pasted one.

    The loopback server is polled in short slices so a paste is noticed within
    POLL_SECONDS rather than after the whole timeout.
    """
    deadline = time.monotonic() + timeout
    while server.result is None:
        if pastes is not None:
            _take_paste(server, pastes)
            if server.result is not None:
                break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            message = f"no authorization arrived within {int(timeout)}s"
            raise FigmaError({"error": "oauth_timeout", "message": message})
        server.timeout = min(remaining, POLL_SECONDS)
        server.handle_request()
        if pastes is not None and server.result is not None:
            print("\nAuthorization received via browser redirect.", file=sys.stderr)
    if "code" not in server.result:
        message = f"Figma returned {server.result['error']}"
        raise FigmaError({"error": "oauth_denied", "message": message})
    return server.result["code"]


def login(
    client: tuple[str, str],
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    timeout: float = CALLBACK_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], Path]:
    """Run the browser flow, validate the token, store it; return (identity, path).

    On a terminal the callback URL can also be pasted, for a browser on another
    machine whose redirect never reaches this loopback server (SSH, containers).
    """
    verifier = generate_code_verifier()
    state = secrets.token_urlsafe(32)
    server = _bind(port, state)
    try:
        challenge = compute_code_challenge(verifier)
        url = authorization_url(client[0], port, state, challenge)
        print(f"Open this URL to authorize figma-cli:\n  {url}", file=sys.stderr)
        if open_browser:
            webbrowser.open(url)
        pastes = None
        if sys.stdin is not None and sys.stdin.isatty():
            print(PASTE_PROMPT, end="", file=sys.stderr, flush=True)
            pastes = _start_paste_reader(sys.stdin)
        code = _await_code(server, timeout, pastes)
    finally:
        server.server_close()
    body = exchange_code(client, code, verifier, port)
    if not body.get("refresh_token"):
        message = f"{TOKEN_PATH} returned no refresh_token"
        raise FigmaError({"error": "invalid_response", "message": message})
    me = checked_me(FigmaClient(body["access_token"], api_base(), bearer=True))
    record = {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "expires_at": _expires_at(body),
        "client_id": client[0],
        "client_secret": client[1],
    }
    return me, save_oauth_token(record)
