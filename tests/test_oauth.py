"""OAuth PKCE flow and token.json lifecycle, over real localhost sockets."""

import base64
import io
import json
import os
import queue
import socket
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from figma_cli import oauth
from figma_cli.cli import main
from figma_cli.client import FigmaClient, FigmaError, UsageError
from tests.test_client import ME, serve

CLIENT = ("client-id", "client-secret")
BASIC = "Basic " + base64.b64encode(b"client-id:client-secret").decode()


class TtyStdin(io.StringIO):
    def isatty(self):
        return True


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _figma_dir(home):
    return home / ".config" / "figma"


def _seed_json(home, **overrides):
    record = {
        "access_token": "figu_stored",
        "refresh_token": "refresh-1",
        "expires_at": int(time.time()) + 3600,
        "client_id": CLIENT[0],
        "client_secret": CLIENT[1],
        **overrides,
    }
    path = _figma_dir(home) / "token.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))
    return path


def _form(body: bytes) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(body.decode()))


def _header(headers: dict[str, str], name: str) -> str | None:
    return next((v for k, v in headers.items() if k.lower() == name.lower()), None)


# PKCE and the authorization URL.


def test_code_challenge_matches_rfc7636_appendix_b():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    expected = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert oauth.compute_code_challenge(verifier) == expected


def test_code_verifier_is_rfc7636_shaped():
    verifier = oauth.generate_code_verifier()
    assert 43 <= len(verifier) <= 128
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert set(verifier) <= allowed
    assert "=" not in oauth.compute_code_challenge(verifier)
    assert oauth.generate_code_verifier() != verifier


def test_authorization_url():
    url = oauth.authorization_url("cid", 54321, "st", "ch")
    parts = urllib.parse.urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == oauth.AUTHORIZE_URL
    assert dict(urllib.parse.parse_qsl(parts.query)) == {
        "client_id": "cid",
        "redirect_uri": "http://127.0.0.1:54321/callback",
        "scope": "current_user:read file_content:read file_comments:read"
        " file_comments:write",
        "state": "st",
        "response_type": "code",
        "code_challenge": "ch",
        "code_challenge_method": "S256",
    }


# Client credentials: flags, then environment, never a built-in default.


def test_client_credentials_resolution(monkeypatch):
    monkeypatch.delenv("FIGMA_CLIENT_ID", raising=False)
    monkeypatch.delenv("FIGMA_CLIENT_SECRET", raising=False)
    assert oauth.client_credentials(None, None) is None
    monkeypatch.setenv("FIGMA_CLIENT_ID", "env-id")
    monkeypatch.setenv("FIGMA_CLIENT_SECRET", "env-secret")
    assert oauth.client_credentials(None, None) == ("env-id", "env-secret")
    assert oauth.client_credentials("flag-id", None) == ("flag-id", "env-secret")
    monkeypatch.delenv("FIGMA_CLIENT_SECRET")
    with pytest.raises(UsageError) as exc:
        oauth.client_credentials(None, None)
    assert exc.value.payload["error"] == "missing_client_credentials"
    assert exc.value.exit_code == 2


# The loopback callback server.


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()


def _callback(port: int, **query) -> tuple[int, str]:
    return _get(f"{oauth.redirect_uri(port)}?{urllib.parse.urlencode(query)}")


def test_callback_rejects_state_mismatch_then_accepts_valid():
    port = _free_port()
    server = oauth._bind(port, "good-state")
    answers = []

    def browser():
        answers.append(_callback(port, code="forged", state="bad-state"))
        answers.append(_get(f"http://127.0.0.1:{port}/elsewhere"))
        answers.append(_callback(port, code="real", state="good-state"))

    thread = threading.Thread(target=browser)
    thread.start()
    try:
        assert oauth._await_code(server, 10) == "real"
    finally:
        thread.join()
        server.server_close()
    assert [status for status, _ in answers] == [400, 404, 200]
    assert "Authorization complete" in answers[2][1]
    assert answers[2][1].startswith("<!doctype html>")


def test_idle_connection_does_not_block_callback(monkeypatch):
    """A preconnected socket that never sends a request times out and is dropped."""
    monkeypatch.setattr(oauth._CallbackHandler, "timeout", 0.3)
    port = _free_port()
    server = oauth._bind(port, "s")
    idle = socket.create_connection(("127.0.0.1", port))
    thread = threading.Thread(target=lambda: _callback(port, code="c", state="s"))
    thread.start()
    try:
        assert oauth._await_code(server, 5) == "c"
    finally:
        idle.close()
        thread.join()
        server.server_close()


def test_callback_error_is_oauth_denied():
    port = _free_port()
    server = oauth._bind(port, "s")
    thread = threading.Thread(
        target=lambda: _callback(port, error="access_denied", state="s")
    )
    thread.start()
    try:
        with pytest.raises(FigmaError) as exc:
            oauth._await_code(server, 10)
    finally:
        thread.join()
        server.server_close()
    assert exc.value.payload["error"] == "oauth_denied"


def test_callback_timeout():
    server = oauth._bind(_free_port(), "s")
    try:
        with pytest.raises(FigmaError) as exc:
            oauth._await_code(server, 0.2)
    finally:
        server.server_close()
    assert exc.value.payload["error"] == "oauth_timeout"


# Pasting the callback into the terminal, for a browser on another machine.


@pytest.mark.parametrize(
    "text",
    [
        "http://127.0.0.1:54321/callback?code=c&state=s",
        "  http://localhost:54321/callback?state=s&code=c\n",
        "code=c&state=s",
        "?code=c&state=s",
    ],
)
def test_paste_accepts_callback_url_or_query(text):
    assert oauth.parse_pasted_callback(text, "s", 54321) == {"code": "c"}


def test_paste_carries_a_denial_through():
    text = "error=access_denied&state=s"
    assert oauth.parse_pasted_callback(text, "s", 54321) == {"error": "access_denied"}


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("c", "no state"),
        ("code=c", "no state"),
        ("code=c&state=forged", "does not match"),
        ("state=s", "no code"),
        ("http://127.0.0.1:54321/elsewhere?code=c&state=s", "expected a URL"),
        ("http://127.0.0.1:9999/callback?code=c&state=s", "expected a URL"),
        ("http://127.0.0.1:bad/callback?code=c&state=s", "expected a URL"),
        ("https://127.0.0.1:54321/callback?code=c&state=s", "expected a URL"),
        ("http://evil.example:54321/callback?code=c&state=s", "expected a URL"),
    ],
)
def test_paste_rejects_what_the_loopback_handler_would(text, reason):
    with pytest.raises(ValueError, match=reason):
        oauth.parse_pasted_callback(text, "s", 54321)


def test_valid_paste_wakes_the_wait_promptly():
    port = _free_port()
    server = oauth._bind(port, "s")
    pastes = queue.SimpleQueue()
    timer = threading.Timer(
        0.2, pastes.put, [f"{oauth.redirect_uri(port)}?code=c&state=s\n"]
    )
    timer.start()
    started = time.monotonic()
    try:
        assert oauth._await_code(server, 30, pastes) == "c"
    finally:
        server.server_close()
    assert time.monotonic() - started < 2


def test_idle_connection_does_not_delay_a_paste():
    """A browser preconnect that never sends must not hold the poll loop."""
    port = _free_port()
    server = oauth._bind(port, "s")
    idle = socket.create_connection(("127.0.0.1", port))
    pastes = queue.SimpleQueue()
    threading.Timer(0.3, pastes.put, ["code=c&state=s"]).start()
    started = time.monotonic()
    try:
        assert oauth._await_code(server, 30, pastes) == "c"
    finally:
        idle.close()
        server.server_close()
    assert time.monotonic() - started < 2  # the handler read timeout is 10s


def test_bad_paste_reprompts_and_loopback_keeps_listening(capsys):
    port = _free_port()
    server = oauth._bind(port, "good-state")
    pastes = queue.SimpleQueue()
    pastes.put("code=forged&state=bad-state\n")
    answers = []

    def browser():
        time.sleep(0.8)  # after the bad paste has been read and rejected
        answers.append(_callback(port, code="real", state="good-state"))

    thread = threading.Thread(target=browser)
    thread.start()
    try:
        assert oauth._await_code(server, 10, pastes) == "real"
    finally:
        thread.join()
        server.server_close()
    assert [status for status, _ in answers] == [200]
    err = capsys.readouterr().err
    assert "does not match this login attempt" in err
    assert err.count(oauth.PASTE_PROMPT) == 1
    assert "Authorization received via browser redirect." in err


@pytest.mark.parametrize("text", ["", "\n\n  \n"])
def test_eof_or_blank_stdin_stops_the_reader_and_keeps_waiting(text):
    pastes = queue.SimpleQueue()
    reader = threading.Thread(
        target=oauth._read_pastes, args=(io.StringIO(text), pastes)
    )
    reader.start()
    reader.join(timeout=1)
    assert not reader.is_alive()
    assert pastes.empty()
    server = oauth._bind(_free_port(), "s")
    try:
        with pytest.raises(FigmaError) as exc:
            oauth._await_code(server, 0.3, pastes)
    finally:
        server.server_close()
    assert exc.value.payload["error"] == "oauth_timeout"


class PasteStdin:
    """A terminal stdin whose one line is the paste, released once it is known."""

    def __init__(self):
        self.lines = queue.SimpleQueue()

    def isatty(self):
        return True

    def readline(self):
        return self.lines.get()


def test_oauth_login_completes_from_a_pasted_callback(home, monkeypatch, capsys):
    """--no-browser over SSH: the redirect never arrives, the paste does."""
    port = _free_port()
    stdin = PasteStdin()
    real_url = oauth.authorization_url

    def authorization_url(client_id, port, state, challenge):
        stdin.lines.put("code=wrong&state=forged\n")
        stdin.lines.put(f"{oauth.redirect_uri(port)}?code=pasted&state={state}\n")
        stdin.lines.put("")  # then EOF
        return real_url(client_id, port, state, challenge)

    opened = []
    monkeypatch.setattr(oauth, "authorization_url", authorization_url)
    monkeypatch.setattr("webbrowser.open", opened.append)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    monkeypatch.setenv("FIGMA_CLIENT_ID", CLIENT[0])
    monkeypatch.setenv("FIGMA_CLIENT_SECRET", CLIENT[1])
    tokens = {"access_token": "figu_new", "refresh_token": "r-new", "expires_in": 90}
    with serve() as (api, rec):
        rec.routes["/v1/oauth/token"] = (200, {}, json.dumps(tokens).encode())
        rec.routes["/v1/me"] = (200, {}, json.dumps(ME).encode())
        monkeypatch.setenv("FIGMA_API_BASE", api)
        started = time.monotonic()
        code = main(["auth", "login", "--no-browser", "--port", str(port), "--json"])
        elapsed = time.monotonic() - started

    captured = capsys.readouterr()
    assert code == 0, captured
    assert elapsed < 2
    assert opened == []
    assert [p for _, p, _ in rec.requests] == ["/v1/oauth/token", "/v1/me"]
    assert _form(rec.bodies[0])["code"] == "pasted"
    stored = json.loads((_figma_dir(home) / "token.json").read_text())
    assert stored["access_token"] == "figu_new"
    assert captured.err.count(oauth.PASTE_PROMPT) == 2
    assert "does not match this login attempt" in captured.err


# The full `figma auth login` browser flow.


def _oauth_login(monkeypatch, api, port, *argv):
    """Run auth login on a TTY; the fake browser follows the authorize URL."""
    seen = {}

    def browser(url):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        seen.update(query)
        target = (
            query["redirect_uri"]
            + "?"
            + urllib.parse.urlencode({"code": "auth-code", "state": query["state"]})
        )
        threading.Thread(target=_get, args=(target,), daemon=True).start()

    monkeypatch.setattr("webbrowser.open", browser)
    monkeypatch.setattr(sys, "stdin", TtyStdin())
    monkeypatch.setenv("FIGMA_API_BASE", api)
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    monkeypatch.setenv("FIGMA_CLIENT_ID", CLIENT[0])
    monkeypatch.setenv("FIGMA_CLIENT_SECRET", CLIENT[1])
    code = main(["auth", "login", "--port", str(port), "--json", *argv])
    return code, seen


def test_oauth_login_exchanges_code_with_basic_auth(home, monkeypatch, capsys):
    port = _free_port()
    tokens = {"access_token": "figu_new", "refresh_token": "r-new", "expires_in": 90}
    with serve() as (api, rec):
        rec.routes["/v1/oauth/token"] = (200, {}, json.dumps(tokens).encode())
        rec.routes["/v1/me"] = (200, {}, json.dumps(ME).encode())
        before = time.time()
        code, seen = _oauth_login(monkeypatch, api, port)

    assert code == 0, capsys.readouterr()
    (method, path, headers), (_, me_path, me_headers) = rec.requests
    assert (method, path) == ("POST", "/v1/oauth/token")
    assert _header(headers, "Authorization") == BASIC
    form = _form(rec.bodies[0])
    assert form == {
        "redirect_uri": f"http://127.0.0.1:{port}/callback",
        "code": "auth-code",
        "grant_type": "authorization_code",
        "code_verifier": form["code_verifier"],
    }
    assert oauth.compute_code_challenge(form["code_verifier"]) == seen["code_challenge"]
    assert seen["client_id"] == CLIENT[0]
    assert me_path == "/v1/me"
    assert _header(me_headers, "Authorization") == "Bearer figu_new"
    assert _header(me_headers, "X-Figma-Token") is None

    path = _figma_dir(home) / "token.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    stored = json.loads(path.read_text())
    assert stored["access_token"] == "figu_new"
    assert stored["refresh_token"] == "r-new"
    assert (stored["client_id"], stored["client_secret"]) == CLIENT
    assert before + 89 <= stored["expires_at"] <= time.time() + 91
    out = json.loads(capsys.readouterr().out)
    assert out == {**ME, "token_path": str(path)}


def test_oauth_login_without_tty_takes_a_pat(home, monkeypatch, capsys):
    monkeypatch.setenv("FIGMA_CLIENT_ID", CLIENT[0])
    monkeypatch.setenv("FIGMA_CLIENT_SECRET", CLIENT[1])
    monkeypatch.setattr(sys, "stdin", io.StringIO("figd_piped\n"))
    with serve() as (api, rec):
        rec.routes["/v1/me"] = (200, {}, json.dumps(ME).encode())
        monkeypatch.setenv("FIGMA_API_BASE", api)
        assert main(["auth", "login", "--json"]) == 0
    assert [p for _, p, _ in rec.requests] == ["/v1/me"]
    assert (_figma_dir(home) / "token").read_text() == "figd_piped\n"


def test_oauth_login_lone_client_id_exits_two(monkeypatch, capsys):
    monkeypatch.delenv("FIGMA_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(sys, "stdin", TtyStdin())
    assert main(["auth", "login", "--client-id", "cid", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "missing_client_credentials"


@pytest.mark.parametrize("argv", [[], ["--port", str(oauth.DEFAULT_PORT)]])
def test_oauth_login_port_collision_exits_two(argv, home, monkeypatch, capsys):
    """The default port 54321, or any --port, held by someone else: exit 2."""
    blocker = socket.socket()
    try:
        blocker.bind(("127.0.0.1", oauth.DEFAULT_PORT))
        blocker.listen()
    except OSError:
        pass  # already held by another process, which is the same collision
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    monkeypatch.setattr(sys, "stdin", TtyStdin())
    monkeypatch.setenv("FIGMA_CLIENT_ID", CLIENT[0])
    monkeypatch.setenv("FIGMA_CLIENT_SECRET", CLIENT[1])
    try:
        code = main(["auth", "login", *argv, "--json"])
    finally:
        blocker.close()
    assert code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "port_unavailable"
    assert "127.0.0.1:54321" in out["message"]
    assert opened == []
    assert not _figma_dir(home).exists()


# Resolution and refresh in FigmaClient.from_env().


def test_from_env_uses_unexpired_token_json_as_bearer(home, monkeypatch, capsys):
    _seed_json(home)
    (_figma_dir(home) / "token").write_text("figd_plaintext\n")
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    with serve() as (api, rec):
        rec.routes["/v1/me"] = (200, {}, json.dumps(ME).encode())
        monkeypatch.setenv("FIGMA_API_BASE", api)
        assert main(["auth", "check", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == ME
    [(_, path, headers)] = rec.requests
    assert _header(headers, "Authorization") == "Bearer figu_stored"
    assert _header(headers, "X-Figma-Token") is None


def test_figma_token_env_outranks_token_json(home, monkeypatch):
    _seed_json(home)
    monkeypatch.setenv("FIGMA_TOKEN", "figd_env")
    client = FigmaClient.from_env()
    assert client.token == "figd_env"
    assert client.auth_headers() == {"X-Figma-Token": "figd_env"}
    monkeypatch.setenv("FIGMA_TOKEN", "figu_env")
    assert FigmaClient.from_env().auth_headers() == {"Authorization": "Bearer figu_env"}


def test_expired_token_refreshes_with_basic_auth(home, monkeypatch, capsys):
    path = _seed_json(home, expires_at=int(time.time()) - 10)
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    refreshed = {"access_token": "figu_fresh", "expires_in": 7776000}
    with serve() as (api, rec):
        rec.routes["/v1/oauth/refresh"] = (200, {}, json.dumps(refreshed).encode())
        rec.routes["/v1/me"] = (200, {}, json.dumps(ME).encode())
        monkeypatch.setenv("FIGMA_API_BASE", api)
        assert main(["auth", "check", "--json"]) == 0
    (_, refresh_path, refresh_headers), (_, _, me_headers) = rec.requests
    assert refresh_path == "/v1/oauth/refresh"
    assert _header(refresh_headers, "Authorization") == BASIC
    assert _form(rec.bodies[0]) == {"refresh_token": "refresh-1"}
    assert _header(me_headers, "Authorization") == "Bearer figu_fresh"
    stored = json.loads(path.read_text())
    assert stored["access_token"] == "figu_fresh"
    assert stored["refresh_token"] == "refresh-1"
    assert stored["expires_at"] > time.time() + 7775000
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert (path.parent / "token.json.lock").exists()


def test_concurrent_refresh_happens_once(home, monkeypatch):
    """Refresh-then-reread under the lock: racing processes refresh once."""
    _seed_json(home, expires_at=0)
    refreshed = {"access_token": "figu_fresh", "expires_in": 3600}
    results = []
    with serve() as (api, rec):
        rec.routes["/v1/oauth/refresh"] = (200, {}, json.dumps(refreshed).encode())
        monkeypatch.setenv("FIGMA_API_BASE", api)
        threads = [
            threading.Thread(target=lambda: results.append(oauth.fresh_access_token()))
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert results == ["figu_fresh"] * 8
    assert [p for _, p, _ in rec.requests] == ["/v1/oauth/refresh"]


def test_refresh_rereads_after_a_sibling_refreshed(home, monkeypatch):
    _seed_json(home, expires_at=0)
    real_lock = oauth._refresh_lock

    def sibling_wins():
        _seed_json(home, access_token="figu_sibling")
        return real_lock()

    monkeypatch.setattr(oauth, "_refresh_lock", sibling_wins)
    monkeypatch.setenv("FIGMA_API_BASE", "http://127.0.0.1:9")  # never called
    assert oauth.fresh_access_token() == "figu_sibling"


def test_refresh_without_fcntl_still_works(home, monkeypatch):
    """Windows has no fcntl: the refresh runs unlocked rather than failing."""
    _seed_json(home, expires_at=0)
    monkeypatch.setitem(sys.modules, "fcntl", None)
    refreshed = {"access_token": "figu_fresh", "expires_in": 3600}
    with serve() as (api, rec):
        rec.routes["/v1/oauth/refresh"] = (200, {}, json.dumps(refreshed).encode())
        monkeypatch.setenv("FIGMA_API_BASE", api)
        assert oauth.fresh_access_token() == "figu_fresh"


def test_refresh_failure_exits_three_without_pat_fallback(home, monkeypatch, capsys):
    path = _seed_json(home, expires_at=0)
    (_figma_dir(home) / "token").write_text("figd_plaintext\n")
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    with serve() as (api, rec):
        body = json.dumps({"message": "Invalid refresh token"}).encode()
        rec.routes["/v1/oauth/refresh"] = (400, {}, body)
        monkeypatch.setenv("FIGMA_API_BASE", api)
        assert main(["auth", "check", "--json"]) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "refresh_failed"
    assert out["status"] == 400
    assert "Invalid refresh token" in out["message"]
    assert [p for _, p, _ in rec.requests] == ["/v1/oauth/refresh"]
    assert json.loads(path.read_text())["access_token"] == "figu_stored"


@pytest.mark.parametrize("content", ["not json", "[]", '{"access_token": "x"}'])
def test_malformed_token_json_is_structured(content, home, monkeypatch, capsys):
    path = _seed_json(home)
    path.write_text(content)
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    assert main(["auth", "check", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["error"] == "token_unreadable"


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_bearer_request_refuses_redirect(status, home, monkeypatch, capsys):
    _seed_json(home)
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    with serve() as (api, api_rec), serve() as (other, other_rec):
        api_rec.routes["/v1/me"] = (status, {"Location": f"{other}/v1/me"}, b"")
        monkeypatch.setenv("FIGMA_API_BASE", api)
        assert main(["auth", "check", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["error"] == "redirect_refused"
    assert len(api_rec.requests) == 1
    assert other_rec.requests == []


def test_refresh_refuses_redirect(home, monkeypatch, capsys):
    _seed_json(home, expires_at=0)
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    with serve() as (api, api_rec), serve() as (other, other_rec):
        location = {"Location": f"{other}/v1/oauth/refresh"}
        api_rec.routes["/v1/oauth/refresh"] = (307, location, b"")
        monkeypatch.setenv("FIGMA_API_BASE", api)
        assert main(["auth", "check", "--json"]) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "refresh_failed"
    assert other_rec.requests == []
