# figma-cli

Headless Figma CLI for AI coding agents and automated design inspection.

Requires Python 3.11 or newer. Licensed under MIT.

## Commands

The package installs two console scripts that run the same entrypoint:

- `figma-cli`
- `figma` (short alias)

Each reports its own name in `--help` and `--version`:

```console
$ figma-cli --version
figma-cli 0.1.0
$ figma --version
figma 0.1.0
```

The npm package `silships/figma-cli` also installs a `figma-cli` executable. If both are installed, whichever directory comes first on `PATH` wins. Run `command -v figma-cli` to check which one you get, or use the `figma` alias.

## Usage

Every command talks to the Figma REST API with a personal access token. Create one under Figma account settings (Security > Personal access tokens) with these scopes, the minimum the commands below need per [Figma's scope reference](https://developers.figma.com/docs/rest-api/scopes/):

| Scope | Used by |
| --- | --- |
| `current_user:read` | `auth check`, `auth login` (`GET /v1/me`) |
| `file_content:read` | `file get`, `export` |
| `file_comments:read` | `comment list` |
| `file_comments:write` | `comment post`, `comment delete` |

Then store it once with `figma auth login`:

```sh
figma auth login                       # interactive: prints the steps, opens settings, hidden prompt
echo "$TOKEN" | figma auth login       # agents and CI: piped stdin
figma auth login --token - < token.txt # same, explicit
```

`auth login` validates the token against `GET /v1/me` before writing anything. A rejected token exits 3 and leaves any existing token file untouched. A valid one is written to `~/.config/figma/token`, created with mode `0600` (and its directory `0700`), and the command reports the authenticated `id`, `handle` and `email` plus the file path. Prefer stdin over `--token <value>`, which exposes the token in process listings and shell history. Empty stdin exits 2. Add `--no-browser` to skip opening the settings page.

Alternatively, export the token. `FIGMA_TOKEN` takes precedence over the stored file whenever it is set and non-empty:

```sh
export FIGMA_TOKEN=figd_...
export FIGMA_API_BASE=https://api.figma.com   # optional, this is the default
```

With neither set, commands fail with `{"error": "missing_token"}` and exit code 3.

Every subcommand prints human-readable text by default and a JSON document on stdout with `--json`.

| Command | Figma endpoint |
| --- | --- |
| `figma auth login [--token TOKEN\|-] [--no-browser]` | `GET /v1/me`, then writes `~/.config/figma/token` |
| `figma auth check` | `GET /v1/me` |
| `figma file get <file_key> [--depth N]` | `GET /v1/files/{file_key}?depth=N` |
| `figma export <file_key> --nodes <ids> [--format png\|svg] [--output DIR]` | `GET /v1/images/{file_key}`, then the asset URLs |
| `figma comment list <file_key>` | `GET /v1/files/{file_key}/comments` |
| `figma comment post <file_key> --message TEXT [--comment-id ID] [--node-id ID]` | `POST /v1/files/{file_key}/comments` |
| `figma comment delete <file_key> <comment_id>` | `DELETE /v1/files/{file_key}/comments/{comment_id}` |

The file key is the segment after `/design/` (or `/file/`) in a Figma URL. Node ids use the `1:2` form; a URL shows them as `node-id=1-2`.

### Examples

Check the token:

```console
$ figma auth check --json
{
  "id": "123456789",
  "handle": "Design Bot",
  "email": "bot@example.com"
}
```

Inspect only the pages of a file. `--depth` is passed to Figma, so the server trims the tree before it is sent:

```sh
figma file get AbCdEf123 --depth 1
figma file get AbCdEf123 --depth 2 --json | jq '.document.children[].name'
```

Render two frames to PNG and print where they were written. Files are named `<file_key>_<node_id>.<format>` with `:` and other unsafe characters replaced by `-`; re-exporting the same node overwrites its file:

```console
$ figma export AbCdEf123 --nodes 1:2,1:3 --output shots
shots/AbCdEf123_1-2.png
shots/AbCdEf123_1-3.png
```

Post a comment pinned to a frame, reply to it, list the thread, then clean up:

```sh
id=$(figma comment post AbCdEf123 --message "Spacing is off" --node-id 1:2 --json | jq -r .id)
figma comment post AbCdEf123 --message "Fixed in the next build" --comment-id "$id"
figma comment list AbCdEf123
figma comment delete AbCdEf123 "$id"
```

### Errors and exit codes

| Exit code | Meaning |
| --- | --- |
| 0 | Success |
| 2 | Usage error (bad or missing arguments) |
| 3 | Figma API, network, or local write failure |

With `--json`, a failure prints a JSON object on stdout; without it, a one-line message goes to stderr. HTTP 401, 403, 404 and 5xx become `unauthorized`, `forbidden`, `not_found` and `server_error`:

```json
{"error": "forbidden", "status": 403, "message": "Invalid token"}
```

HTTP 429 is reported at once, never retried, so the caller decides when to try again. `retry_after` comes from the `Retry-After` header and is `null` when Figma sends none:

```json
{"error": "rate_limit_exceeded", "status": 429, "retry_after": 30}
```

### Token safety

A request carrying `X-Figma-Token` follows no redirects. Any 3xx answer is refused with `{"error": "redirect_refused"}` and exit code 3 before a second request is made, so the token never reaches another host. Exported images are downloaded from the pre-signed URLs Figma returns with a separate request that carries no token; only that request follows redirects.

## Installation and quick run

Run on-demand without installing via [uv](https://docs.astral.sh/uv/):

```sh
uvx figma-cli --help
uvx figma-cli auth check --json
```

Or install from [PyPI](https://pypi.org/project/figma-cli/):

```sh
pip install figma-cli
```

From a local checkout:

```sh
uvx --from . figma-cli --version
```

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest tests/
```

The tests are hermetic: no network access and no Figma token are needed, and `HOME` points at a temporary directory so a stored token never leaks in. The redirect tests use real sockets on `127.0.0.1`.

`tests/test_live.py` runs against the real API only when `FIGMA_TOKEN` and `FIGMA_TEST_FILE_KEY` are set (add `FIGMA_TEST_NODE_ID` to exercise export). It posts one comment and deletes it, and runs `auth login` into a temporary `HOME`. CI runs it in the `live` job from the `FIGMA_TOKEN` secret and the `FIGMA_TEST_FILE_KEY` / `FIGMA_TEST_NODE_ID` repository variables, and skips it when they are absent.

## Release

Bump `__version__` in `src/figma_cli/__init__.py`, merge, then push a matching tag (`v0.1.0` for `0.1.0`). The release workflow refuses a tag that does not match `__version__`, builds the sdist and wheel, runs `twine check`, and publishes to PyPI through trusted publishing (OIDC, the `pypi` environment).

## License

MIT, see [LICENSE](LICENSE).
