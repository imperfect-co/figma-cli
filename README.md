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

## Run without installing

Before the first PyPI release, run straight from the repository with [uv](https://docs.astral.sh/uv/):

```sh
uvx --from git+https://github.com/imperfect-co/figma-cli figma-cli --help
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

The tests are hermetic: no network access and no Figma token are needed.

## Release

Bump `__version__` in `src/figma_cli/__init__.py`, merge, then push a matching tag (`v0.1.0` for `0.1.0`). The release workflow refuses a tag that does not match `__version__`, builds the sdist and wheel, runs `twine check`, and publishes to PyPI through trusted publishing (OIDC, the `pypi` environment).

## License

MIT, see [LICENSE](LICENSE).
