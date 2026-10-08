# CLI Commands

The package installs two console scripts, `figma-cli` and `figma`, that run the same entrypoint. This page uses `figma`.

```text
figma [-h] [--version] <command> ...
```

Every subcommand accepts `--json`: without it, it prints human-readable text; with it, it prints a JSON document on stdout. Running `figma` with no command prints the help and exits 0.

| Command | Figma endpoint |
| --- | --- |
| `figma auth check` | `GET /v1/me` |
| `figma auth login [--token TOKEN\|-] [--no-browser]` | `GET /v1/me`, then writes `~/.config/figma/token` |
| `figma auth login [--client-id ID] [--client-secret SECRET] [--port PORT]` | OAuth: `POST /v1/oauth/token`, `GET /v1/me`, then writes `~/.config/figma/token.json` |
| `figma file get <file_key> [--depth N]` | `GET /v1/files/{file_key}?depth=N` |
| `figma node get <file_key> --nodes <ids> [--depth N] [--geometry paths]` | `GET /v1/files/{file_key}/nodes?ids=<ids>&depth=N&geometry=paths` |
| `figma variable list <file_key> [--published]` | `GET /v1/files/{file_key}/variables/local`, or `/variables/published` with `--published` |
| `figma export <file_key> --nodes <ids> [--format png\|svg] [--output DIR]` | `GET /v1/images/{file_key}`, then the asset URLs |
| `figma comment list <file_key>` | `GET /v1/files/{file_key}/comments` |
| `figma comment post <file_key> --message TEXT [--comment-id ID] [--node-id ID]` | `POST /v1/files/{file_key}/comments` |
| `figma comment delete <file_key> <comment_id>` | `DELETE /v1/files/{file_key}/comments/{comment_id}` |

The file key is the segment after `/design/` (or `/file/`) in a Figma URL. Node ids use the `1:2` form; a URL shows them as `node-id=1-2`.

## auth check

```text
figma auth check [--json]
```

Validates the credential that the [resolution order](auth.md#resolution-order) picks, by calling `GET /v1/me`.

```console
$ figma auth check
Authenticated as Design Bot <bot@example.com> (123456789)
$ figma auth check --json
{
  "id": "123456789",
  "handle": "Design Bot",
  "email": "bot@example.com"
}
```

## auth login

```text
figma auth login [--json] [--token TOKEN] [--browser | --no-browser]
                 [--client-id CLIENT_ID] [--client-secret CLIENT_SECRET] [--port PORT]
```

Obtains a credential, validates it against `GET /v1/me`, and stores it. See [Authentication](auth.md) for both flows.

| Flag | Meaning |
| --- | --- |
| `--token TOKEN` | Personal access token value, or `-` to read stdin. Piped stdin is read without this flag; prefer it, since a literal value shows in process listings and shell history. |
| `--browser`, `--no-browser` | Open the Figma settings page or the OAuth page when interactive. Default: open. |
| `--client-id CLIENT_ID` | OAuth app client id. Default: `$FIGMA_CLIENT_ID`. |
| `--client-secret CLIENT_SECRET` | OAuth app client secret. Default: `$FIGMA_CLIENT_SECRET`. Prefer the variable. |
| `--port PORT` | Loopback port for the OAuth redirect `http://127.0.0.1:PORT/callback`, 1 to 65535. Default: `54321`. |

The JSON output adds the path that was written:

```json
{
  "id": "123456789",
  "handle": "Design Bot",
  "email": "bot@example.com",
  "token_path": "/home/me/.config/figma/token"
}
```

## file get

```text
figma file get [--json] [--depth DEPTH] file_key
```

Fetches a file's node tree. `--depth` (a positive integer) is passed to Figma, so the server trims the tree before it is sent. The text output is a header with the file name and last-modified time, then one indented `TYPE name (id)` line per node. The JSON output is Figma's file response, unchanged.

```sh
figma file get AbCdEf123 --depth 1
figma file get AbCdEf123 --depth 2 --json | jq '.document.children[].name'
```

## node get

```text
figma node get [--json] --nodes NODES [--depth DEPTH] [--geometry {paths}] file_key
```

Fetches only the subtrees you need instead of the whole file.

| Flag | Meaning |
| --- | --- |
| `--nodes NODES` | Required. Comma-separated node ids. |
| `--depth DEPTH` | Server-side subtree depth, a positive integer. |
| `--geometry paths` | Add vector path data to each node. |

The text output is the file header, then each requested node's tree in the order given. Ids Figma cannot find come back as `null` in the JSON and as a `not found` line in the text output; the command still exits 0:

```console
$ figma node get AbCdEf123 --nodes 1:2,9:9 --depth 1
Spec (last modified 2026-01-01T00:00:00Z)
FRAME Card (1:2)
  TEXT Title (1:3)
Node 9:9: not found
```

With `--json`, the response is Figma's, unchanged, keyed by node id under `nodes`.

## variable list

```text
figma variable list [--json] [--published] file_key
```

Lists the design variables in a file, one collection per block with each variable's resolved type and its value in every mode. Colors print as hex, aliases as `-> <name>`, or `-> <id>` when the aliased variable is not in the response (such as one from a library). An extended collection inherits its parent's variables and values, with its own overrides applied per mode.

```console
$ figma variable list AbCdEf123
Colors (VariableCollectionId:1:1) [modes: Light, Dark]
  brand/primary  COLOR  Light=#FF0000  Dark=#00000080
  text/default  COLOR  Light=-> brand/primary  Dark=-> VariableID:9:9
  radius  FLOAT  Light=4  Dark=8
```

`--published` lists the variables this file publishes to its library instead; Figma returns no per-mode values for those. A file with none prints `No variables.`. With `--json`, the response is Figma's, unchanged.

!!! warning "Figma Enterprise only"
    The Variables REST API is available only to full members of Figma Enterprise organizations, with a token carrying `file_variables:read`. Anywhere else the command exits 3 with `{"error": "forbidden", "status": 403}`.

## export

```text
figma export [--json] --nodes NODES [--format {png,svg}] [--output OUTPUT] file_key
```

Renders nodes to image files.

| Flag | Meaning |
| --- | --- |
| `--nodes NODES` | Required. Comma-separated node ids. |
| `--format {png,svg}` | Image format. Default: `png`. |
| `--output OUTPUT` | Destination directory, created if missing. Default: the current directory. |

Files are named `<file_key>_<node_id>.<format>`, with `:` and any other character outside `A-Z a-z 0-9 _ . -` replaced by `-`. Re-exporting the same node overwrites its file. The text output lists one path per line:

```console
$ figma export AbCdEf123 --nodes 1:2,1:3 --output shots
shots/AbCdEf123_1-2.png
shots/AbCdEf123_1-3.png
```

With `--json`:

```json
{
  "files": [
    {"node_id": "1:2", "path": "shots/AbCdEf123_1-2.png", "bytes": 48213},
    {"node_id": "1:3", "path": "shots/AbCdEf123_1-3.png", "bytes": 51877}
  ]
}
```

If Figma returns no image URL for any requested node, the command writes nothing and fails with `render_failed`, listing the nodes:

```json
{"error": "render_failed", "message": "no image URL returned", "nodes": ["1:3"]}
```

The images are downloaded from the pre-signed URLs Figma returns, with a separate request that carries no token. See [redirect safety](client.md#redirect-safety).

## comment list

```text
figma comment list [--json] file_key
```

Lists a file's comments, one per line as `id  created_at  handle: message`, with `(reply to <id>)` after the handle for replies, or `No comments.`. The JSON output is Figma's response, unchanged (`{"comments": [...]}`).

## comment post

```text
figma comment post [--json] --message MESSAGE [--comment-id COMMENT_ID] [--node-id NODE_ID] file_key
```

| Flag | Meaning |
| --- | --- |
| `--message MESSAGE` | Required. The comment text. |
| `--comment-id COMMENT_ID` | Reply to this comment thread. |
| `--node-id NODE_ID` | Anchor the comment to this node. |

The text output is `Posted comment <id>`; the JSON output is the comment Figma created.

## comment delete

```text
figma comment delete [--json] file_key comment_id
```

The text output is `Deleted comment <id>`. With `--json`:

```json
{"deleted": true, "id": "987654"}
```

### A comment round trip

Post a comment pinned to a frame, reply to it, list the thread, then clean up:

```sh
id=$(figma comment post AbCdEf123 --message "Spacing is off" --node-id 1:2 --json | jq -r .id)
figma comment post AbCdEf123 --message "Fixed in the next build" --comment-id "$id"
figma comment list AbCdEf123
figma comment delete AbCdEf123 "$id"
```

## Exit codes

| Exit code | Meaning |
| --- | --- |
| 0 | Success |
| 2 | Usage error: bad or missing arguments, empty or malformed token input, a lone OAuth client credential, a busy OAuth port |
| 3 | Figma API, network, or local write failure |

## Errors

With `--json`, a failure prints a JSON object on stdout. Without it, a one-line message goes to stderr:

```console
$ figma file get AbCdEf123
figma: error: forbidden (HTTP 403): Invalid token
```

Argument errors detected while parsing the command line (a missing required flag, a bad `--depth`) are reported by the argument parser on stderr as a usage message, with exit code 2, even with `--json`.

HTTP errors map to these names:

| HTTP status | `error` |
| --- | --- |
| 400 | `bad_request` |
| 401 | `unauthorized` |
| 403 | `forbidden` |
| 404 | `not_found` |
| 429 | `rate_limit_exceeded` |
| 5xx | `server_error` |
| any other | `http_error` |

```json
{"error": "forbidden", "status": 403, "message": "Invalid token"}
```

HTTP 429 is reported at once, never retried, so the caller decides when to try again. `retry_after` comes from the `Retry-After` header (in seconds, or an HTTP date converted to seconds) and is `null` when Figma sends none:

```json
{"error": "rate_limit_exceeded", "status": 429, "retry_after": 30}
```

Other errors you may see, all with exit code 3 unless noted:

| `error` | Cause |
| --- | --- |
| `missing_token` | No `FIGMA_TOKEN` and no stored token file. |
| `token_unreadable` | A stored token file could not be read or is malformed. |
| `network_error` | The request did not reach Figma (DNS, connection, timeout). |
| `invalid_response` | Figma answered with something other than the expected JSON. |
| `redirect_refused` | A token-bearing request was answered with a redirect. See [redirect safety](client.md#redirect-safety). |
| `render_failed` | `export` got no image URL for a node. |
| `invalid_asset_url` | `export` got an image URL that is not `http` or `https`. |
| `write_failed` | A token file or exported image could not be written. |
| `refresh_failed` | Figma rejected the OAuth token refresh. Run `figma auth login` again. |
| `oauth_timeout`, `oauth_denied` | The OAuth login timed out or was denied in the browser. |
| `empty_token`, `invalid_token` | Exit 2. `auth login` got no token, or one with spaces, line breaks or non-ASCII characters. |
| `missing_client_credentials` | Exit 2. Only one of the OAuth client id and secret is set. |
| `port_unavailable` | Exit 2. The OAuth loopback port is busy. |
