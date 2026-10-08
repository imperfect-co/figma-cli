"""Minimal Figma REST client built on the standard library.

Two transport rules hold for every request:

* A request carrying a credential (``X-Figma-Token`` for a personal access token,
  ``Authorization: Bearer`` for an OAuth token, ``Authorization: Basic`` for OAuth
  client credentials) follows zero redirects. Any 3xx answer is refused before a
  second request is made, so the credential never reaches another host.
* Rendered assets are fetched from their pre-signed URLs without the token.

Credentials resolve in this order: ``FIGMA_TOKEN``, then the OAuth tokens in
``~/.config/figma/token.json`` (refreshed when expired), then the personal access
token in ``~/.config/figma/token``.
"""

import email.utils
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_API_BASE = "https://api.figma.com"
TOKEN_HEADER = "X-Figma-Token"
OAUTH_TOKEN_PREFIX = "figu_"
TIMEOUT_SECONDS = 60
EXIT_USAGE = 2
EXIT_API_ERROR = 3

_ERROR_NAMES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    429: "rate_limit_exceeded",
}


class FigmaError(Exception):
    """A failed Figma call, carrying the structured payload the CLI prints."""

    exit_code = EXIT_API_ERROR

    def __init__(self, payload: dict[str, Any]):
        super().__init__(payload.get("message") or payload["error"])
        self.payload = payload


class UsageError(FigmaError):
    """A local usage problem (bad input, busy port): exit 2."""

    exit_code = EXIT_USAGE

    def __init__(self, error: str, message: str):
        super().__init__({"error": error, "message": message})


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FigmaError(
            {
                "error": "redirect_refused",
                "status": code,
                "message": "refusing to follow a redirect on a token-bearing request",
            }
        )


def parse_retry_after(value: str | None, now: float | None = None) -> int | None:
    """Return Retry-After as whole seconds, from delta-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    return max(0, round(when.timestamp() - now))


def _http_error(err: urllib.error.HTTPError) -> FigmaError:
    status = err.code
    if status == 429:
        return FigmaError(
            {
                "error": "rate_limit_exceeded",
                "status": 429,
                "retry_after": parse_retry_after(err.headers.get("Retry-After")),
            }
        )
    name = _ERROR_NAMES.get(status, "server_error" if status >= 500 else "http_error")
    payload: dict[str, Any] = {"error": name, "status": status}
    try:
        body = json.loads(err.read() or b"{}")
    except (ValueError, OSError):
        body = {}
    message = body.get("err") or body.get("message") if isinstance(body, dict) else None
    if message:
        payload["message"] = str(message)
    return FigmaError(payload)


def _network_error(err: OSError) -> FigmaError:
    reason = getattr(err, "reason", err)
    return FigmaError({"error": "network_error", "message": str(reason)})


def token_path() -> Path:
    """Where ``figma auth login`` stores a personal access token."""
    return Path.home() / ".config" / "figma" / "token"


def token_json_path() -> Path:
    """Where ``figma auth login`` stores OAuth tokens and client credentials."""
    return token_path().with_name("token.json")


def is_oauth_token(token: str) -> bool:
    """True for a Figma OAuth access token, which travels as a Bearer token."""
    return token.startswith(OAUTH_TOKEN_PREFIX)


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` at mode 0600, replacing it only once written.

    The new file is created 0600 under a fresh name (O_EXCL, so never through a
    planted file or symlink), then renamed over the old one: a failed write
    leaves the previous file intact, and nothing is ever readable by others.
    """
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)  # refuses a directory owned by someone else
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            _fsync_dir(path.parent)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    except OSError as err:
        raise FigmaError({"error": "write_failed", "message": str(err)}) from None


def _fsync_dir(directory: Path) -> None:
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def read_token_file() -> str:
    """Return the stored token, or "" when no regular file holds one."""
    path = token_path()
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as err:
        raise FigmaError({"error": "token_unreadable", "message": str(err)}) from None


def api_base() -> str:
    return os.environ.get("FIGMA_API_BASE", "").strip() or DEFAULT_API_BASE


def _quote(segment: str) -> str:
    return urllib.parse.quote(segment, safe="")


class FigmaClient:
    """Token-bearing client for the Figma REST API."""

    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_API_BASE,
        timeout: float = TIMEOUT_SECONDS,
        bearer: bool | None = None,
    ):
        self.token = token
        self.bearer = is_oauth_token(token) if bearer is None else bearer
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_RefuseRedirects)

    @classmethod
    def from_env(cls) -> "FigmaClient":
        """Resolve FIGMA_TOKEN, then stored OAuth tokens, then the stored PAT."""
        token = os.environ.get("FIGMA_TOKEN", "").strip()
        if token:
            return cls(token, api_base())
        if token_json_path().exists():
            from figma_cli.oauth import fresh_access_token

            return cls(fresh_access_token(), api_base(), bearer=True)
        token = read_token_file()
        if not token:
            message = (
                f"FIGMA_TOKEN is not set and {token_path()} holds no token;"
                " run 'figma auth login'"
            )
            raise FigmaError({"error": "missing_token", "message": message})
        return cls(token, api_base())

    def auth_headers(self) -> dict[str, str]:
        if self.bearer:
            return {"Authorization": f"Bearer {self.token}"}
        return {TOKEN_HEADER: self.token}

    def request(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {**self.auth_headers(), "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as err:
            raise _http_error(err) from None
        except OSError as err:
            raise _network_error(err) from None
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise FigmaError(
                {"error": "invalid_response", "message": "response is not JSON"}
            ) from None

    def me(self) -> dict[str, Any]:
        return self.request("GET", "/v1/me")

    def get_file(self, file_key: str, depth: int | None = None) -> dict[str, Any]:
        query = {"depth": depth} if depth is not None else None
        return self.request("GET", f"/v1/files/{_quote(file_key)}", query)

    def get_nodes(
        self,
        file_key: str,
        node_ids: list[str],
        depth: int | None = None,
        geometry: str | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {"ids": ",".join(node_ids)}
        if depth is not None:
            query["depth"] = depth
        if geometry:
            query["geometry"] = geometry
        return self.request("GET", f"/v1/files/{_quote(file_key)}/nodes", query)

    def get_variables(self, file_key: str, published: bool = False) -> dict[str, Any]:
        """Enterprise only: other plans (or a token without file_variables:read) 403."""
        scope = "published" if published else "local"
        return self.request("GET", f"/v1/files/{_quote(file_key)}/variables/{scope}")

    def get_images(
        self, file_key: str, node_ids: list[str], fmt: str
    ) -> dict[str, str | None]:
        query = {"ids": ",".join(node_ids), "format": fmt}
        resp = self.request("GET", f"/v1/images/{_quote(file_key)}", query)
        if resp.get("err"):
            raise FigmaError({"error": "render_failed", "message": str(resp["err"])})
        return resp.get("images") or {}

    def list_comments(self, file_key: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/files/{_quote(file_key)}/comments")

    def post_comment(
        self,
        file_key: str,
        message: str,
        comment_id: str | None = None,
        node_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"message": message}
        if comment_id:
            body["comment_id"] = comment_id
        if node_id:
            body["client_meta"] = {"node_id": node_id, "node_offset": {"x": 0, "y": 0}}
        return self.request("POST", f"/v1/files/{_quote(file_key)}/comments", body=body)

    def delete_comment(self, file_key: str, comment_id: str) -> Any:
        path = f"/v1/files/{_quote(file_key)}/comments/{_quote(comment_id)}"
        return self.request("DELETE", path)


def checked_me(client: FigmaClient) -> dict[str, Any]:
    """GET /v1/me, insisting on a user id: proof the credential works."""
    me = client.me()
    if not isinstance(me, dict) or not me.get("id"):
        message = "GET /v1/me returned no user id"
        raise FigmaError({"error": "invalid_response", "message": message})
    return me


def download(url: str, timeout: float = TIMEOUT_SECONDS) -> bytes:
    """Fetch a pre-signed asset URL. Sends no token, so redirects are safe to follow."""
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise FigmaError({"error": "invalid_asset_url", "message": url})
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as err:
        raise _http_error(err) from None
    except OSError as err:
        raise _network_error(err) from None
