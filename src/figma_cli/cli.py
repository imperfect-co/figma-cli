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

Handler = Callable[[FigmaClient, argparse.Namespace], tuple[Any, str]]


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _auth_check(client: FigmaClient, args: argparse.Namespace) -> tuple[Any, str]:
    me = client.me()
    ident = {key: me.get(key) for key in ("id", "handle", "email")}
    return (
        ident,
        f"Authenticated as {ident['handle']} <{ident['email']}> ({ident['id']})",
    )


def _tree_lines(node: dict[str, Any], indent: int = 0) -> list[str]:
    label = f"{node.get('type', '?')} {node.get('name', '')} ({node.get('id', '')})"
    lines = ["  " * indent + label]
    for child in node.get("children") or []:
        lines.extend(_tree_lines(child, indent + 1))
    return lines


def _file_get(client: FigmaClient, args: argparse.Namespace) -> tuple[Any, str]:
    data = client.get_file(args.file_key, args.depth)
    header = f"{data.get('name', '')} (last modified {data.get('lastModified', '?')})"
    tree = _tree_lines(data["document"]) if data.get("document") else []
    return data, "\n".join([header, *tree])


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
        path = out_dir / f"{_safe_name(node_id)}.{args.format}"
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
        epilog="Environment: FIGMA_TOKEN (required), FIGMA_API_BASE (optional).",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print structured JSON")
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    auth = commands.add_parser("auth", help="token checks")
    auth_cmds = auth.add_subparsers(dest="action", metavar="<action>", required=True)
    p = auth_cmds.add_parser("check", parents=[common], help="validate FIGMA_TOKEN")
    p.set_defaults(handler=_auth_check)

    file = commands.add_parser("file", help="file inspection")
    file_cmds = file.add_subparsers(dest="action", metavar="<action>", required=True)
    p = file_cmds.add_parser("get", parents=[common], help="fetch the node tree")
    p.add_argument("file_key")
    p.add_argument("--depth", type=_positive_int, help="server-side tree depth")
    p.set_defaults(handler=_file_get)

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
        payload, text = handler(FigmaClient.from_env(), args)
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
