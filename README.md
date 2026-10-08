# figma-cli

Headless Figma CLI for AI coding agents and automated design inspection.

Inspect Figma files, export nodes to PNG or SVG, and manage comments from a terminal, CI job or agent, with JSON output and stable exit codes. Zero runtime dependencies; requires Python 3.11 or newer.

**Documentation: https://figma-cli.readthedocs.io/**

## Install

```sh
uvx figma-cli --help     # run without installing
pip install figma-cli    # or install from PyPI
```

The package installs two equivalent commands, `figma-cli` and the short alias `figma`.

## Quickstart

```sh
figma auth login                                  # store a personal access token
figma auth check --json
figma file get AbCdEf123 --depth 1
figma export AbCdEf123 --nodes 1:2 --output shots
figma comment list AbCdEf123
```

Authentication (personal access tokens and OAuth), the full command reference, exit codes, the Python API and the development guide are on the [documentation site](https://figma-cli.readthedocs.io/).

## License

MIT, see [LICENSE](LICENSE).
