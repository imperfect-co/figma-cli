# Python API

The CLI is a thin layer over `figma_cli.client.FigmaClient`, a minimal Figma REST client built on the standard library. You can import it directly.

## Creating a client

`FigmaClient.from_env()` resolves a credential the same way the CLI does: `FIGMA_TOKEN`, then the OAuth tokens in `~/.config/figma/token.json` (refreshed when expired), then the personal access token in `~/.config/figma/token`. It honours `FIGMA_API_BASE`. With no credential it raises `FigmaError` with `missing_token`.

```python
from figma_cli.client import FigmaClient

client = FigmaClient.from_env()
print(client.me()["handle"])
```

Or pass a token yourself:

```python
client = FigmaClient("figd_...", base_url="https://api.figma.com", timeout=60)
```

| Parameter | Meaning |
| --- | --- |
| `token` | The credential. |
| `base_url` | API base URL. Default: `https://api.figma.com`. |
| `timeout` | Socket timeout in seconds. Default: `60`. |
| `bearer` | Send the token as `Authorization: Bearer` instead of `X-Figma-Token`. Default: `None`, which means bearer exactly when the token starts with `figu_` (an OAuth access token). |

## Endpoint methods

| Method | Request | Returns |
| --- | --- | --- |
| `me()` | `GET /v1/me` | The user object. |
| `get_file(file_key, depth=None)` | `GET /v1/files/{file_key}` | The file document. |
| `get_nodes(file_key, node_ids, depth=None, geometry=None)` | `GET /v1/files/{file_key}/nodes` | `{"nodes": {...}}` keyed by node id, `null` for an id Figma cannot find. |
| `get_variables(file_key, published=False)` | `GET /v1/files/{file_key}/variables/local` (or `/published`) | The variables payload. Figma Enterprise only; elsewhere it raises `FigmaError` with `forbidden`. |
| `get_images(file_key, node_ids, fmt)` | `GET /v1/images/{file_key}` | A dict of node id to pre-signed image URL (or `None` for a node Figma could not render). |
| `list_comments(file_key)` | `GET /v1/files/{file_key}/comments` | `{"comments": [...]}` |
| `post_comment(file_key, message, comment_id=None, node_id=None)` | `POST /v1/files/{file_key}/comments` | The created comment. `comment_id` makes it a reply; `node_id` anchors it to a node. |
| `delete_comment(file_key, comment_id)` | `DELETE /v1/files/{file_key}/comments/{comment_id}` | The decoded response body, `{}` when empty. |
| `request(method, path, query=None, body=None)` | Any endpoint | The decoded JSON response, `{}` for an empty body. |

Path segments are percent-encoded, so a file key or comment id cannot change the request path.

`figma_cli.client.download(url, timeout=60)` fetches a pre-signed asset URL, such as one returned by `get_images`, and returns its bytes. It sends no credential and accepts only `http` and `https` URLs.

```python
from figma_cli.client import FigmaClient, download

client = FigmaClient.from_env()
urls = client.get_images("AbCdEf123", ["1:2"], "png")
with open("frame.png", "wb") as fh:
    fh.write(download(urls["1:2"]))
```

## Redirect safety

A request that carries a credential follows no redirects. Every `FigmaClient` builds its opener with `_RefuseRedirects`, a `urllib.request.HTTPRedirectHandler` that raises `FigmaError` with `redirect_refused` (and the 3xx `status`) instead of issuing a second request. The OAuth token calls, which send the client credentials as `Authorization: Basic`, use the same handler. So no credential (`X-Figma-Token`, `Authorization: Bearer` or `Authorization: Basic`) is ever forwarded to another host.

Rendered images are fetched by `download()`, which carries no token, so only that request follows redirects.

## Error handling

Every failure raises `figma_cli.client.FigmaError`. It carries:

- `payload`: the structured dict the CLI prints with `--json`, always with an `error` key, and `status`, `message` or `retry_after` where they apply.
- `exit_code`: `3` for API, network and write failures. The subclass `UsageError` (bad input, a busy port) uses `2`.

```python
from figma_cli.client import FigmaClient, FigmaError

client = FigmaClient.from_env()
try:
    data = client.get_file("AbCdEf123", depth=1)
except FigmaError as err:
    if err.payload["error"] == "rate_limit_exceeded":
        print("retry after", err.payload["retry_after"], "seconds")
    else:
        raise
```

The `error` names and their HTTP statuses are listed under [Errors](commands.md#errors). HTTP 429 is raised at once and never retried.
