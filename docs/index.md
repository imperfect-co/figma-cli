# figma-cli

Headless Figma CLI for AI coding agents and automated design inspection.

figma-cli reads Figma files, renders nodes to images and manages file comments from a terminal, a CI job or an agent's tool call. Every command prints human-readable text by default and a JSON document on stdout with `--json`, and every failure maps to a stable exit code and a structured error payload.

## Zero runtime dependencies

The package declares `dependencies = []`. The CLI and the Python client are built on the standard library alone (`urllib`, `http.server`, `json`), so installing figma-cli pulls nothing else into your environment. It requires Python 3.11 or newer and is licensed under MIT.

## Features

- **Inspection:** fetch a file's node tree, trimmed server side with `--depth`, or only the node subtrees you need.
- **Variables:** list a file's design variable collections with their values in every mode (Figma Enterprise).
- **Export:** render nodes to PNG or SVG files on disk.
- **Comments:** list, post (including replies and node-anchored comments) and delete file comments.
- **Two ways to authenticate:** a personal access token, or OAuth 2.0 with PKCE through your own Figma OAuth app, with automatic refresh. See [Authentication](auth.md).
- **Token safety:** a request that carries a credential never follows a redirect, so the credential cannot reach another host. See [Python API](client.md#redirect-safety).
- **A Python client:** the same `FigmaClient` the CLI uses is importable. See [Python API](client.md).

## Installation

Run on demand without installing, via [uv](https://docs.astral.sh/uv/):

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

The package installs two console scripts that run the same entrypoint: `figma-cli` and the short alias `figma`. Each reports its own name in `--help` and `--version`.

!!! note "Name clash with the npm package"
    The npm package `silships/figma-cli` also installs a `figma-cli` executable. If both are installed, whichever directory comes first on `PATH` wins. Run `command -v figma-cli` to check which one you get, or use the `figma` alias.

## Quickstart

Store a personal access token once (see [Authentication](auth.md) for the scopes it needs), then inspect a file:

```sh
figma auth login                                  # hidden prompt for the token
figma auth check --json                           # who am I?
figma file get AbCdEf123 --depth 1                # list the pages of a file
figma node get AbCdEf123 --nodes 1:2              # fetch one frame's subtree
figma export AbCdEf123 --nodes 1:2 --output shots # render a frame to PNG
figma comment list AbCdEf123
```

The file key is the segment after `/design/` (or `/file/`) in a Figma URL. Node ids use the `1:2` form; a URL shows them as `node-id=1-2`.

The full reference is in [CLI Commands](commands.md).
