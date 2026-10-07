"""Console entrypoint shared by the ``figma-cli`` and ``figma`` commands."""

import argparse
from collections.abc import Sequence

from figma_cli import __version__


def build_parser() -> argparse.ArgumentParser:
    # prog is left unset so it follows sys.argv[0]: each alias reports its own name.
    parser = argparse.ArgumentParser(
        description="Headless Figma CLI for AI coding agents and automated design inspection.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
