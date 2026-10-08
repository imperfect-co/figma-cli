"""Console entrypoint shared by the ``figma-cli`` and ``figma`` commands.

Exit codes: 0 success, 2 usage error, 3 Figma API, network or local write failure.
"""

import argparse
import json
import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from figma_cli import __version__
from figma_cli.client import FigmaClient, FigmaError, download
from figma_cli.login import login
from figma_cli.oauth import DEFAULT_PORT

# Most handlers take (client, args); those marked needs_client=False take (args).
Handler = Callable[..., tuple[Any, str]]


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _identity(me: dict[str, Any]) -> tuple[dict[str, Any], str]:
    ident = {key: me.get(key) for key in ("id", "handle", "email")}
    return (
        ident,
        f"Authenticated as {ident['handle']} <{ident['email']}> ({ident['id']})",
    )


def _auth_check(client: FigmaClient, args: argparse.Namespace) -> tuple[Any, str]:
    return _identity(client.me())


def _auth_login(args: argparse.Namespace) -> tuple[Any, str]:
    me, path = login(args)
    ident, text = _identity(me)
    return {**ident, "token_path": str(path)}, f"{text}\nToken saved to {path}"


def _tree_lines(node: dict[str, Any], indent: int = 0) -> list[str]:
    label = f"{node.get('type', '?')} {node.get('name', '')} ({node.get('id', '')})"
    lines = ["  " * indent + label]
    for child in node.get("children") or []:
        lines.extend(_tree_lines(child, indent + 1))
    return lines


def _file_header(data: dict[str, Any]) -> str:
    return f"{data.get('name', '')} (last modified {data.get('lastModified', '?')})"


def _file_get(client: FigmaClient, args: argparse.Namespace) -> tuple[Any, str]:
    data = client.get_file(args.file_key, args.depth)
    tree = _tree_lines(data["document"]) if data.get("document") else []
    return data, "\n".join([_file_header(data), *tree])


def _node_get(client: FigmaClient, args: argparse.Namespace) -> tuple[Any, str]:
    data = client.get_nodes(args.file_key, args.nodes, args.depth, args.geometry)
    nodes = data.get("nodes") or {}
    lines = [_file_header(data)]
    for node_id in args.nodes:
        document = (nodes.get(node_id) or {}).get("document")
        if document:
            lines.extend(_tree_lines(document))
        else:
            lines.append(f"Node {node_id}: not found")
    return data, "\n".join(lines)


def _format_value(value: Any, names: dict[str, str]) -> str:
    if isinstance(value, dict) and value.get("type") == "VARIABLE_ALIAS":
        return "-> " + names.get(value.get("id", ""), str(value.get("id")))
    if isinstance(value, dict) and {"r", "g", "b"} <= value.keys():
        channels = [value["r"], value["g"], value["b"]]
        if value.get("a", 1) != 1:
            channels.append(value["a"])
        return "#" + "".join(f"{round(c * 255):02X}" for c in channels)
    return json.dumps(value, ensure_ascii=False)


_UNSET = object()


class _Variables:
    """A variables/local (or published) payload, resolved for text output.

    An extended collection inherits its parent's variables: each of its modes
    names a parentModeId, and variableOverrides replaces values per mode.
    """

    def __init__(self, meta: dict[str, Any]):
        self.variables = meta.get("variables") or {}
        self.collections = meta.get("variableCollections") or {}
        self.names = {i: v.get("name", "") for i, v in self.variables.items()}
        self.parents: dict[str, str | None] = {}
        self.overrides: dict[str, dict[str, Any]] = {}
        for coll in self.collections.values():
            for mode in coll.get("modes") or []:
                self.parents[mode.get("modeId")] = mode.get("parentModeId")
            for var_id, by_mode in (coll.get("variableOverrides") or {}).items():
                self.overrides.setdefault(var_id, {}).update(by_mode)

    def members(self, coll_id: str, coll: dict[str, Any]) -> list[str]:
        """Own and inherited ids; published collections list none, so match."""
        own = coll.get("variableIds") or []
        ids = list(dict.fromkeys([*own, *(coll.get("inheritedVariableIds") or [])]))
        if ids:
            return [i for i in ids if i in self.variables]
        return [
            i
            for i, v in self.variables.items()
            if v.get("variableCollectionId") == coll_id
        ]

    def value(self, var_id: str, mode_id: str | None) -> Any:
        """An override, else the variable's own value, else the parent mode's."""
        own = self.variables[var_id].get("valuesByMode") or {}
        override = self.overrides.get(var_id, {})
        seen = set()
        while mode_id and mode_id not in seen:
            seen.add(mode_id)
            for source in (override, own):
                if mode_id in source:
                    return source[mode_id]
            mode_id = self.parents.get(mode_id)
        return _UNSET

    def lines(self) -> list[str]:
        lines = []
        for coll_id, coll in self.collections.items():
            modes = [(m.get("modeId"), m.get("name")) for m in coll.get("modes") or []]
            names = ", ".join(name for _, name in modes)
            lines.append(
                f"{coll.get('name', '')} ({coll_id})" + (names and f" [modes: {names}]")
            )
            for var_id in self.members(coll_id, coll):
                lines.append(self._line(var_id, modes))
        return lines

    def _line(self, var_id: str, modes: list[tuple[Any, Any]]) -> str:
        var = self.variables[var_id]
        kind = var.get("resolvedType") or var.get("resolvedDataType") or "?"
        cells = [var.get("name", ""), kind]
        for mode_id, name in modes:
            value = self.value(var_id, mode_id)
            if value is not _UNSET:
                cells.append(f"{name}={_format_value(value, self.names)}")
        return "  " + "  ".join(cells)


def _variable_list(client: FigmaClient, args: argparse.Namespace) -> tuple[Any, str]:
    data = client.get_variables(args.file_key, args.published)
    lines = _Variables(data.get("meta") or {}).lines()
    return data, "\n".join(lines) or "No variables."


def _safe_name(node_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", node_id)


def _export(client: FigmaClient, args: argparse.Namespace) -> tuple[Any, str]:
    node_ids = args.nodes
    images = client.get_images(args.file_key, node_ids, args.format)
    missing = [n for n in node_ids if not images.get(n)]
    if missing:
        raise FigmaError(
            {
                "error": "render_failed",
                "message": "no image URL returned",
                "nodes": missing,
            }
        )
    out_dir = Path(args.output)
    files = []
    for node_id in node_ids:
        payload = download(images[node_id])
        path = (
            out_dir / f"{_safe_name(args.file_key)}_{_safe_name(node_id)}.{args.format}"
        )
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        except OSError as err:
            raise FigmaError({"error": "write_failed", "message": str(err)}) from None
        files.append({"node_id": node_id, "path": str(path), "bytes": len(payload)})
    return {"files": files}, "\n".join(f["path"] for f in files)


def _comment_line(c: dict[str, Any]) -> str:
    user = (c.get("user") or {}).get("handle", "?")
    reply = f" (reply to {c['parent_id']})" if c.get("parent_id") else ""
    head = f"{c.get('id')}  {c.get('created_at', '')}  {user}{reply}"
    return f"{head}: {c.get('message', '')}"


def _comment_list(client: FigmaClient, args: argparse.Namespace) -> tuple[Any, str]:
    data = client.list_comments(args.file_key)
    lines = [_comment_line(c) for c in data.get("comments") or []]
    return data, "\n".join(lines) or "No comments."


def _comment_post(client: FigmaClient, args: argparse.Namespace) -> tuple[Any, str]:
    data = client.post_comment(
        args.file_key, args.message, comment_id=args.comment_id, node_id=args.node_id
    )
    return data, f"Posted comment {data.get('id')}"


def _comment_delete(client: FigmaClient, args: argparse.Namespace) -> tuple[Any, str]:
    client.delete_comment(args.file_key, args.comment_id)
    return {
        "deleted": True,
        "id": args.comment_id,
    }, f"Deleted comment {args.comment_id}"


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _port(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return number


def _node_list(value: str) -> list[str]:
    nodes = [n.strip() for n in value.split(",") if n.strip()]
    if not nodes:
        raise argparse.ArgumentTypeError("expected one or more node ids")
    return nodes


def build_parser() -> argparse.ArgumentParser:
    # prog is left unset so it follows sys.argv[0]: each alias reports its own name.
    parser = argparse.ArgumentParser(
        description=(
            "Headless Figma CLI for AI coding agents and automated design inspection."
        ),
        epilog=(
            "Environment: FIGMA_TOKEN (optional when 'figma auth login' has stored"
            " ~/.config/figma/token.json or ~/.config/figma/token), FIGMA_API_BASE"
            " (optional), FIGMA_CLIENT_ID and FIGMA_CLIENT_SECRET (OAuth login)."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print structured JSON")
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    auth = commands.add_parser("auth", help="token setup and checks")
    auth_cmds = auth.add_subparsers(dest="action", metavar="<action>", required=True)
    p = auth_cmds.add_parser(
        "check", parents=[common], help="validate FIGMA_TOKEN or stored token file"
    )
    p.set_defaults(handler=_auth_check)
    p = auth_cmds.add_parser(
        "login",
        parents=[common],
        help=(
            "authorize via OAuth (with client credentials) or store a personal"
            " access token, after validating it"
        ),
    )
    p.add_argument(
        "--token",
        help=(
            "token value, or '-' to read stdin; a literal value shows in ps and"
            " shell history, so prefer piped stdin, which is read without this flag"
        ),
    )
    p.add_argument(
        "--browser",
        action=argparse.BooleanOptionalAction,
        help=("open Figma settings or the OAuth page when interactive (default: open)"),
    )
    p.add_argument(
        "--client-id", help="OAuth app client id (default: $FIGMA_CLIENT_ID)"
    )
    p.add_argument(
        "--client-secret",
        help=(
            "OAuth app client secret (default: $FIGMA_CLIENT_SECRET); a literal"
            " value shows in ps and shell history, so prefer the variable"
        ),
    )
    p.add_argument(
        "--port",
        type=_port,
        default=DEFAULT_PORT,
        help=(
            "loopback port for the OAuth redirect http://127.0.0.1:PORT/callback"
            f" (default: {DEFAULT_PORT})"
        ),
    )
    p.set_defaults(handler=_auth_login, needs_client=False)

    file = commands.add_parser("file", help="file inspection")
    file_cmds = file.add_subparsers(dest="action", metavar="<action>", required=True)
    p = file_cmds.add_parser("get", parents=[common], help="fetch the node tree")
    p.add_argument("file_key")
    p.add_argument("--depth", type=_positive_int, help="server-side tree depth")
    p.set_defaults(handler=_file_get)

    node = commands.add_parser("node", help="node inspection")
    node_cmds = node.add_subparsers(dest="action", metavar="<action>", required=True)
    p = node_cmds.add_parser("get", parents=[common], help="fetch node subtrees")
    p.add_argument("file_key")
    p.add_argument(
        "--nodes", required=True, type=_node_list, help="comma-separated node ids"
    )
    p.add_argument("--depth", type=_positive_int, help="server-side subtree depth")
    p.add_argument("--geometry", choices=("paths",), help="include vector path data")
    p.set_defaults(handler=_node_get)

    variable = commands.add_parser(
        "variable", help="design variables (Figma Enterprise only)"
    )
    variable_cmds = variable.add_subparsers(
        dest="action", metavar="<action>", required=True
    )
    p = variable_cmds.add_parser(
        "list", parents=[common], help="list variable collections and values"
    )
    p.add_argument("file_key")
    p.add_argument(
        "--published",
        action="store_true",
        help="list variables published from this file instead of local ones",
    )
    p.set_defaults(handler=_variable_list)

    p = commands.add_parser("export", parents=[common], help="render nodes to files")
    p.add_argument("file_key")
    p.add_argument(
        "--nodes", required=True, type=_node_list, help="comma-separated node ids"
    )
    p.add_argument("--format", choices=("png", "svg"), default="png")
    p.add_argument("--output", default=".", help="destination directory")
    p.set_defaults(handler=_export)

    comment = commands.add_parser("comment", help="file comments")
    comment_cmds = comment.add_subparsers(
        dest="action", metavar="<action>", required=True
    )
    p = comment_cmds.add_parser("list", parents=[common], help="list comments")
    p.add_argument("file_key")
    p.set_defaults(handler=_comment_list)
    p = comment_cmds.add_parser("post", parents=[common], help="post a comment")
    p.add_argument("file_key")
    p.add_argument("--message", required=True)
    p.add_argument("--comment-id", help="reply to this comment thread")
    p.add_argument("--node-id", help="anchor the comment to this node")
    p.set_defaults(handler=_comment_post)
    p = comment_cmds.add_parser("delete", parents=[common], help="delete a comment")
    p.add_argument("file_key")
    p.add_argument("comment_id")
    p.set_defaults(handler=_comment_delete)
    return parser


def _describe(error: dict[str, Any]) -> str:
    text = error["error"]
    if "status" in error:
        text += f" (HTTP {error['status']})"
    if error.get("message"):
        text += f": {error['message']}"
    if error.get("retry_after") is not None:
        text += f", retry after {error['retry_after']}s"
    return text


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Handler | None = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    try:
        if getattr(args, "needs_client", True):
            payload, text = handler(FigmaClient.from_env(), args)
        else:
            payload, text = handler(args)
    except FigmaError as err:
        if args.json:
            _print_json(err.payload)
        else:
            print(f"{parser.prog}: error: {_describe(err.payload)}", file=sys.stderr)
        return err.exit_code
    if args.json:
        _print_json(payload)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
