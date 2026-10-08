"""``figma auth login``: obtain a token, validate it, then store it.

The token is checked against ``GET /v1/me`` before anything touches disk, so a
rejected token never replaces a working one. The OAuth browser flow lives in
``figma_cli.oauth``.
"""

import argparse
import getpass
import sys
import webbrowser
from pathlib import Path

from figma_cli import oauth
from figma_cli.client import (
    FigmaClient,
    FigmaError,
    UsageError,
    api_base,
    checked_me,
    token_json_path,
    token_path,
    write_private,
)

SETTINGS_URL = "https://www.figma.com/settings"
APPS_URL = "https://www.figma.com/developers/apps"
REDIRECT_URL = f"http://127.0.0.1:{oauth.DEFAULT_PORT}{oauth.CALLBACK_PATH}"
SCOPES = oauth.SCOPES
SCOPE_LINES = "\n".join(f"       {scope}" for scope in SCOPES)
APP_INSTRUCTIONS = f"""\
For 1-click browser login, create an OAuth app at {APPS_URL}
with redirect URL {REDIRECT_URL} and grant these scopes:
{SCOPE_LINES}
Enter its client id and secret below to save them to
{{path}} (mode 0600), or export FIGMA_CLIENT_ID and
FIGMA_CLIENT_SECRET instead. Press Enter to skip and use a personal access token.
"""
INSTRUCTIONS = f"""\
Create a personal access token for figma-cli:
  1. Open {SETTINGS_URL} and go to Security > Personal access tokens.
  2. Generate a new token with these scopes:
{SCOPE_LINES}
  3. Paste the token below (input is hidden).
"""


class TokenInputError(UsageError):
    """A missing or malformed token on input: a usage error, exit 2."""


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
        raise TokenInputError("empty_token", "no token was provided")
    if any(not "!" <= ch <= "~" for ch in token):
        message = "token must be visible ASCII, without spaces or line breaks"
        raise TokenInputError("invalid_token", message)
    return token


def save_token(token: str) -> Path:
    """Store a personal access token at mode 0600 and drop any OAuth tokens.

    ``token.json`` outranks the plaintext file, so leaving it in place would let
    stale OAuth tokens silently shadow the token just saved.
    """
    path = token_path()
    write_private(path, token + "\n")
    try:
        token_json_path().unlink(missing_ok=True)
    except OSError as err:
        raise FigmaError({"error": "write_failed", "message": str(err)}) from None
    return path


def prompt_app_config() -> tuple[str, str] | None:
    """Offer once to save the user's OAuth app; None when either half is skipped."""
    print(APP_INSTRUCTIONS.format(path=oauth.app_config_path()), file=sys.stderr)
    print("OAuth client id: ", end="", file=sys.stderr, flush=True)
    client_id = sys.stdin.readline().strip()
    if not client_id:
        return None
    client_secret = getpass.getpass("OAuth client secret: ").strip()
    if not client_secret:
        return None
    oauth.save_app_config(client_id, client_secret)
    return client_id, client_secret


def login(args: argparse.Namespace) -> tuple[dict, Path]:
    """Obtain and validate a credential, store it, and return (identity, path).

    An interactive terminal with OAuth client credentials (flags, environment,
    or app.json) runs the browser flow; without them it offers to save an OAuth
    app first. Everything else (--token, piped stdin, a skipped offer) takes a PAT.
    """
    if args.token is None and sys.stdin.isatty():
        client = oauth.client_credentials(args.client_id, args.client_secret)
        client = client or prompt_app_config()
        if client:
            return oauth.login(client, args.port, args.browser is not False)
    token = candidate_token(args)
    me = checked_me(FigmaClient(token, api_base()))
    return me, save_token(token)
