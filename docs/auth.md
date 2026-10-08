# Authentication

Every command talks to the Figma REST API with one of two credentials: a personal access token, or an OAuth token issued to your own Figma OAuth app. `figma auth login` obtains either one, validates it against `GET /v1/me`, and only then stores it.

## Scopes

Grant the token (or the OAuth app) these scopes, the minimum the commands need per [Figma's scope reference](https://developers.figma.com/docs/rest-api/scopes/):

| Scope | Used by |
| --- | --- |
| `current_user:read` | `auth check`, `auth login` (`GET /v1/me`) |
| `file_content:read` | `file get`, `export` |
| `file_comments:read` | `comment list` |
| `file_comments:write` | `comment post`, `comment delete` |

## Personal access tokens

Create a personal access token under Figma account settings (Security > Personal access tokens) with the scopes above, then store it once:

```sh
figma auth login                       # interactive: prints the steps, opens settings, hidden prompt
echo "$TOKEN" | figma auth login       # agents and CI: piped stdin
figma auth login --token - < token.txt # same, explicit
```

`auth login` takes this personal access token path whenever `--token` is given, stdin is piped, or no OAuth client credentials are configured.

- The token is validated against `GET /v1/me` before anything is written. A rejected token exits 3 and leaves any existing token file untouched.
- Empty input exits 2 with `empty_token`. A token containing spaces, line breaks or non-ASCII characters exits 2 with `invalid_token`.
- Prefer stdin over `--token <value>`: a literal value shows in process listings and shell history.
- `--no-browser` skips opening the Figma settings page.
- Saving a personal access token also removes `~/.config/figma/token.json`, so stale OAuth tokens never shadow it.

On success the command reports the authenticated `id`, `handle` and `email` plus the path it wrote (`token_path` in the `--json` output).

## OAuth 2.0 with PKCE (bring your own app)

figma-cli ships no OAuth app of its own: Figma authenticates the client with its secret on every token call, and a secret embedded in an open-source package is not a secret. Bring your own:

1. Create an OAuth app in the [Figma developer console](https://www.figma.com/developers/apps) and register the redirect URL `http://127.0.0.1:54321/callback`. Grant it the four scopes above.
2. Export its credentials and log in from an interactive terminal:

```sh
export FIGMA_CLIENT_ID=...
export FIGMA_CLIENT_SECRET=...
figma auth login                 # opens the browser, waits on 127.0.0.1:54321
figma auth login --port 8765     # if you registered http://127.0.0.1:8765/callback instead
```

The browser flow runs only when stdin is an interactive terminal, `--token` is not given, and client credentials are configured. Otherwise `auth login` falls back to the personal access token path.

- `--client-id` and `--client-secret` override the variables, but a literal secret shows in process listings and shell history.
- Setting only one of the two exits 2 with `missing_client_credentials`.
- The flow uses PKCE (S256) and a random `state`. A callback with the wrong `state` is answered with HTTP 400 and ignored.
- Figma matches redirect URLs exactly, so a busy port is not swapped for another one: the command exits 2 with `port_unavailable`.
- `--no-browser` prints the authorization URL without opening it.
- With no authorization within 300 seconds the command exits 3 with `oauth_timeout`. Denying access in the browser exits 3 with `oauth_denied`.

### Remote terminals: paste the callback URL

Over SSH, in a container, or anywhere the browser runs on another machine, the browser's redirect to `127.0.0.1` never reaches figma-cli. While it waits, the command also reads the terminal. After authorizing, copy the full URL from the browser's address bar (the page itself fails to load) and paste it at the prompt:

```text
Paste the callback URL here (or finish in the browser):
```

A bare `code=...&state=...` query string works too. A pasted URL must point at `http://127.0.0.1` (or `localhost`) on the configured port with the path `/callback`; a bare query string has no host or path to check. Either way `state` must match this login attempt, so a bare code without `state` is refused, and the paste must then carry a `code` (or the `error` Figma sent). A rejected paste is explained, the prompt returns, and the loopback server keeps listening.

### What is stored, and refresh

The access token is checked against `GET /v1/me`. Then the access token, refresh token, expiry, client id and client secret are written to `~/.config/figma/token.json`.

Figma access tokens last 90 days. When the stored one has expired (or is within 60 seconds of expiring), the next command refreshes it through `POST /v1/oauth/refresh` before running. The refresh holds an advisory lock on `~/.config/figma/token.json.lock` and re-reads the file once it has the lock, so on Linux and macOS concurrent commands refresh once rather than invalidating each other's tokens. Windows has no `fcntl`, so there the refresh runs unlocked and concurrent commands can each refresh, invalidating the access token another just received.

A refresh that Figma rejects exits 3 with `refresh_failed` rather than falling back to an older personal access token. Run `figma auth login` again.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `FIGMA_TOKEN` | A token to use instead of the stored files. A value starting `figu_` (an OAuth access token) is sent as `Authorization: Bearer`, anything else as `X-Figma-Token`. |
| `FIGMA_CLIENT_ID` | OAuth app client id for `auth login` (overridden by `--client-id`). |
| `FIGMA_CLIENT_SECRET` | OAuth app client secret for `auth login` (overridden by `--client-secret`). |
| `FIGMA_API_BASE` | API base URL. Optional; defaults to `https://api.figma.com`. |

```sh
export FIGMA_TOKEN=figd_...
```

## Resolution order

Every command except `auth login` resolves its credential in this order:

1. `FIGMA_TOKEN`, when set and non-empty.
2. `~/.config/figma/token.json`, the OAuth tokens, refreshed when expired.
3. `~/.config/figma/token`, the personal access token.

With none of them, commands fail with `missing_token` and exit code 3.

## Token storage

Both files are written atomically with mode `0600`, inside `~/.config/figma` with mode `0700`:

- The new contents go to a fresh temporary file, created exclusively (never through a planted file or symlink) with mode `0600`.
- The file is flushed and synced, then renamed over the old one. A failed write leaves the previous token intact and exits 3 with `write_failed`.
- At no point is the token readable by other users.
