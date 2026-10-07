"""Minimal Figma REST client built on the standard library.

Two transport rules hold for every request:

* A request carrying ``X-Figma-Token`` follows zero redirects. Any 3xx answer is
  refused before a second request is made, so the token never reaches another host.
* Rendered assets are fetched from their pre-signed URLs without the token.
"""

import email.utils
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_API_BASE = "https://api.figma.com"
TOKEN_HEADER = "X-Figma-Token"
TIMEOUT_SECONDS = 60
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


def _quote(segment: str) -> str:
    return urllib.parse.quote(segment, safe="")


class FigmaClient:
    """Token-bearing client for the Figma REST API."""

    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_API_BASE,
        timeout: float = TIMEOUT_SECONDS,
    ):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_RefuseRedirects)

    @classmethod
    def from_env(cls) -> "FigmaClient":
        token = os.environ.get("FIGMA_TOKEN", "").strip()
        if not token:
            raise FigmaError(
                {"error": "missing_token", "message": "FIGMA_TOKEN is not set"}
            )
        base = os.environ.get("FIGMA_API_BASE", "").strip() or DEFAULT_API_BASE
        return cls(token, base)

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
        headers = {TOKEN_HEADER: self.token, "Accept": "application/json"}
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
