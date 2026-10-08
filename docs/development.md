# Development

## Setup

```sh
git clone https://github.com/imperfect-co/figma-cli.git
cd figma-cli
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

The `dev` extra installs `pytest`, `ruff`, `build` and `twine`. The runtime itself has no dependencies.

## Lint and test

```sh
ruff check .
ruff format --check .
pytest tests/
```

The hermetic suite (`tests/test_cli.py`, `tests/test_client.py`, `tests/test_oauth.py`) needs no network access and no Figma token. `HOME` points at a temporary directory, so a stored token never leaks in. The redirect tests use real sockets on `127.0.0.1`.

## Live tests

`tests/test_live.py` runs against the real API. Each check skips unless its variables are set: most need `FIGMA_TOKEN`, while the OAuth refresh check needs only its three OAuth variables.

| Variable | Enables |
| --- | --- |
| `FIGMA_TOKEN` | `auth check`, and `auth login` from stdin into a temporary `HOME`. |
| `FIGMA_TEST_FILE_KEY` | The file and comment checks. The comment check posts one comment and deletes it. |
| `FIGMA_TEST_NODE_ID` | The export check (together with `FIGMA_TEST_FILE_KEY`). |
| `FIGMA_OAUTH_REFRESH_TOKEN`, `FIGMA_CLIENT_ID`, `FIGMA_CLIENT_SECRET` | The OAuth refresh check. Each refresh invalidates the previous access token for that app, so use an OAuth app kept for testing only. |

CI runs them in the `live` job from the `FIGMA_TOKEN`, `FIGMA_OAUTH_REFRESH_TOKEN`, `FIGMA_CLIENT_ID` and `FIGMA_CLIENT_SECRET` secrets and the `FIGMA_TEST_FILE_KEY` and `FIGMA_TEST_NODE_ID` repository variables, and the tests skip cleanly when those are absent.

## Documentation

This site is built with [Zensical](https://zensical.org/) from `zensical.toml` and the Markdown files in `docs/`:

```sh
pip install -e ".[docs]"
zensical serve              # live preview at http://localhost:8000
zensical build --strict     # the same build Read the Docs runs; output in site/
```

`--strict` turns warnings, such as a broken internal link or a page missing from the navigation, into a failed build. Read the Docs builds the site from `.readthedocs.yaml` on every change to `main`.

## Release

Bump `__version__` in `src/figma_cli/__init__.py`, merge, then push a matching tag (`v0.1.0` for `0.1.0`). The release workflow (`.github/workflows/release.yml`) refuses a tag that does not match `__version__`, builds the sdist and wheel, runs `twine check`, and publishes to PyPI through trusted publishing (OIDC, the `pypi` environment).
