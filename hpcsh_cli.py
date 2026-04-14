#!/usr/bin/env python3

import argparse
import sys
from datetime import datetime
from pathlib import Path

from protocol import META_FILE, STATE_FILE, list_session_dirs, read_json, shared_root_from_env


def cmd_ls() -> None:
    root = shared_root_from_env()
    if not root.is_dir():
        print(f"hpcsh: HPCSH_ROOT is not a directory: {root}", file=sys.stderr)
        sys.exit(2)
    names = sorted(
        p.name
        for p in root.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )
    for name in names:
        print(name)


def _session_id_from_path(session_dir: Path) -> str:
    name = session_dir.name
    prefix = "session_"
    return name[len(prefix) :] if name.startswith(prefix) else name


def _fmt_created_ms(ms: object) -> str:
    if not isinstance(ms, int) or ms <= 0:
        return "-"
    try:
        return datetime.fromtimestamp(ms / 1000.0).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "-"


def cmd_sessions(node: str) -> None:
    root = shared_root_from_env()
    node_dir = root / node
    if not node_dir.is_dir():
        print(f"hpcsh: no such node directory: {node_dir}", file=sys.stderr)
        sys.exit(2)

    rows = []
    for session_dir in list_session_dirs(node_dir):
        sid = _session_id_from_path(session_dir)
        meta = read_json(session_dir / META_FILE, default={})
        state = read_json(session_dir / STATE_FILE, default={})
        st = str(state.get("state", "?"))
        user = str(meta.get("user", "-"))
        created = _fmt_created_ms(meta.get("created_ms"))
        rows.append((sid, st, user, created))

    if not rows:
        return

    w_id = max(len(r[0]) for r in rows)
    w_id = max(w_id, len("SESSION_ID"))
    w_st = max(len(r[1]) for r in rows)
    w_st = max(w_st, len("STATE"))
    w_user = max(len(r[2]) for r in rows)
    w_user = max(w_user, len("USER"))

    header = f"{'SESSION_ID'.ljust(w_id)}  {'STATE'.ljust(w_st)}  {'USER'.ljust(w_user)}  CREATED"
    print(header)
    print(f"{'-' * len(header)}")
    for sid, st, user, created in rows:
        print(f"{sid.ljust(w_id)}  {st.ljust(w_st)}  {user.ljust(w_user)}  {created}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="hpcsh", description="Shared-disk offline HPC terminal")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ls", help="List node directories under shared root ($HPCSH_ROOT or cwd)")

    p_sess = sub.add_parser(
        "sessions",
        help="List sessions on a node (use id with: hpcsh login NODE --session ID)",
    )
    p_sess.add_argument("node", help="Node name, e.g. node01")

    p_server = sub.add_parser(
        "server",
        help="Run PTY server on a compute node (shared root: $HPCSH_ROOT or cwd)",
    )
    p_server.add_argument("--node", required=True, help="Node name, e.g. node01")
    p_server.add_argument(
        "--keep-sessions-on-exit",
        action="store_true",
        help="Keep session_* dirs when server stops (default: remove them on exit)",
    )

    p_login = sub.add_parser(
        "login",
        help="Connect client to a node (shared root: $HPCSH_ROOT or current directory)",
    )
    p_login.add_argument("node", help="Node name, e.g. node01")
    p_login.add_argument(
        "--session",
        metavar="ID",
        help="Reuse session id (see: hpcsh sessions NODE)",
    )
    p_login.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Keep session directory on exit for debugging/reconnect",
    )

    args = parser.parse_args()

    if args.command == "ls":
        cmd_ls()
        return

    if args.command == "sessions":
        cmd_sessions(args.node)
        return

    if args.command == "server":
        import hpcsh_server

        server_argv = ["--node", args.node]
        if args.keep_sessions_on_exit:
            server_argv.append("--keep-sessions-on-exit")
        hpcsh_server.main(server_argv)
        return

    if args.command == "login":
        import hpcsh_client

        login_argv = ["--node", args.node]
        if args.session:
            login_argv += ["--session", args.session]
        if args.no_cleanup:
            login_argv += ["--no-cleanup"]
        hpcsh_client.main(login_argv)
        return


if __name__ == "__main__":
    main()
