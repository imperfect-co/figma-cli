"""``figma auth login``: obtain a token, validate it, then store it.

The token is checked against ``GET /v1/me`` before anything touches disk, so a
rejected token never replaces a working one.
"""

import argparse
import getpass
import os
import secrets
import sys
import webbrowser
from pathlib import Path

from figma_cli.client import EXIT_USAGE, FigmaClient, FigmaError, api_base, token_path

SETTINGS_URL = "https://www.figma.com/settings"
SCOPES = (
    "current_user:read",
    "file_content:read",
    "file_comments:read",
    "file_comments:write",
)
INSTRUCTIONS = f"""\
Create a personal access token for figma-cli:
  1. Open {SETTINGS_URL} and go to Security > Personal access tokens.
  2. Generate a new token with these scopes:
{chr(10).join(f"       {scope}" for scope in SCOPES)}
  3. Paste the token below (input is hidden).
"""


class EmptyToken(FigmaError):
    exit_code = EXIT_USAGE

    def __init__(self):
        super().__init__({"error": "empty_token", "message": "no token was provided"})


def _prompt(open_browser: bool) -> str:
    print(INSTRUCTIONS, file=sys.stderr)
    if open_browser:
        webbrowser.open(SETTINGS_URL)
    return getpass.getpass("Figma personal access token: ")


def candidate_token(args: argparse.Namespace) -> str:
    """Take the token from --token, from stdin, or from a hidden prompt."""
    if args.token is not None and args.token != "-":
        token = args.token
    elif args.token == "-" or not sys.stdin.isatty():
        token = sys.stdin.read()
    else:
        token = _prompt(args.browser is not False)
    token = token.strip()
    if not token:
        raise EmptyToken()
    return token


def save_token(token: str) -> Path:
    """Store the token at mode 0600, replacing any old file only once it is written.

    The new file is created 0600 under a fresh name (O_EXCL, so never through a
    planted file or symlink), then renamed over the old one: a failed write
    leaves the previous token intact, and nothing is ever readable by others.
    """
    path = token_path()
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)  # refuses a directory owned by someone else
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    except OSError as err:
        raise FigmaError({"error": "write_failed", "message": str(err)}) from None
    return path


def login(args: argparse.Namespace) -> tuple[dict, Path]:
    """Validate a candidate token, store it, and return (identity, path)."""
    token = candidate_token(args)
    me = FigmaClient(token, api_base()).me()
    return me, save_token(token)
