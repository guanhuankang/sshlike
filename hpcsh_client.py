#!/usr/bin/env python3

import argparse
import codecs
import getpass
import os
import select
import shutil
import signal
import sys
import termios
import time
import tty
import unicodedata
from pathlib import Path

from protocol import (
    CLIENT_POLL_INTERVAL,
    EVENT_LOG_FILE,
    IN_LOG_FILE,
    META_FILE,
    OUT_LOG_FILE,
    PROTOCOL_VERSION,
    STATE_FILE,
    append_ndjson,
    atomic_write_json,
    b64_decode,
    b64_encode,
    ensure_session_layout,
    make_session_id,
    now_ms,
    read_ndjson_incremental,
    shared_root_from_env,
)


HEARTBEAT_INTERVAL = 2.0


def _char_display_width(ch: str) -> int:
    if ord(ch) < 128:
        return 1
    ea = unicodedata.east_asian_width(ch)
    return 2 if ea in ("W", "F") else 1


class TerminalMode:
    def __enter__(self):
        if not sys.stdin.isatty():
            self.enabled = False
            return self
        self.enabled = True
        self.fd = sys.stdin.fileno()
        self.old_settings = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        return self

    def __exit__(self, exc_type, exc, tb):
        if getattr(self, "enabled", False):
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_settings)


def get_tty_size():
    cols, rows = shutil.get_terminal_size(fallback=(80, 24))
    return rows, cols


def create_session(session_dir: Path, node: str, cleanup: bool) -> None:
    ensure_session_layout(session_dir)
    rows, cols = get_tty_size()
    meta = {
        "protocol": PROTOCOL_VERSION,
        "created_ms": now_ms(),
        "user": getpass.getuser(),
        "node": node,
        "rows": rows,
        "cols": cols,
        "cleanup_on_exit": cleanup,
        "client_pid": os.getpid(),
    }
    state = {"protocol": PROTOCOL_VERSION, "state": "idle", "created_ms": now_ms(), "updated_ms": now_ms()}
    atomic_write_json(session_dir / META_FILE, meta)
    atomic_write_json(session_dir / STATE_FILE, state)


def append_input(session_dir: Path, seq: int, evt_type: str, **kwargs):
    payload = {"seq": seq, "ts_ms": now_ms(), "type": evt_type}
    payload.update(kwargs)
    append_ndjson(session_dir / IN_LOG_FILE, payload)


def _esc_seq_complete(seq: bytes) -> bool:
    if not seq or seq[0] != 0x1B:
        return True
    if len(seq) == 1:
        return False
    if seq[1] == ord("["):
        # CSI ends with a byte in 0x40–0x7e (not '[' itself).
        return len(seq) >= 3 and 0x40 <= seq[-1] <= 0x7E and seq[-1] != ord("[")
    if seq[1] == ord("O"):
        return len(seq) >= 3
    return len(seq) >= 2


def run_interactive(session_dir: Path) -> int:
    in_seq = 0
    out_offset = 0
    running = True
    saw_exit = False
    exit_code = 0
    # Avoid sending heartbeat on the first loop tick (was last_heartbeat=0 → immediate write).
    last_heartbeat = time.time()
    pending_resize = True

    def send_resize():
        nonlocal in_seq
        rows, cols = get_tty_size()
        in_seq += 1
        append_input(session_dir, in_seq, "resize", rows=rows, cols=cols)

    def on_winch(_sig, _frame):
        nonlocal pending_resize
        pending_resize = True

    old_handler = signal.getsignal(signal.SIGWINCH)
    signal.signal(signal.SIGWINCH, on_winch)

    # Line not yet sent to shared disk: local edit + local echo only (no disk on backspace).
    line_chars: list[str] = []
    esc_buf = bytearray()
    utf8_dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
    out_fd = sys.stdout.fileno()

    def disk_stdin(payload: bytes) -> None:
        nonlocal in_seq
        if not payload:
            return
        in_seq += 1
        append_input(session_dir, in_seq, "stdin", data_b64=b64_encode(payload))

    def line_bytes() -> bytes:
        return "".join(line_chars).encode("utf-8")

    def flush_line_with_suffix(suffix: bytes) -> None:
        nonlocal line_chars
        payload = line_bytes() + suffix
        line_chars.clear()
        disk_stdin(payload)

    def local_echo(ch: str) -> None:
        os.write(out_fd, ch.encode("utf-8"))

    def backspace_local() -> None:
        if not line_chars:
            return
        ch = line_chars.pop()
        w = _char_display_width(ch)
        os.write(out_fd, (("\b \b") * w).encode())

    def handle_u_char(ch: str) -> None:
        nonlocal esc_buf
        o = ord(ch)

        if esc_buf:
            if ch in ("\x08", "\x7f"):
                if esc_buf:
                    esc_buf.pop()
                return
            if ch in ("\n", "\r", "\t"):
                flush_line_with_suffix(bytes(esc_buf) + ch.encode("utf-8"))
                esc_buf.clear()
                return
            esc_buf.extend(ch.encode("utf-8"))
            if len(esc_buf) > 48:
                flush_line_with_suffix(bytes(esc_buf))
                esc_buf.clear()
                return
            if _esc_seq_complete(bytes(esc_buf)):
                flush_line_with_suffix(bytes(esc_buf))
                esc_buf.clear()
            return

        if ch == "\x1b":
            esc_buf.extend(b"\x1b")
            if _esc_seq_complete(bytes(esc_buf)):
                flush_line_with_suffix(bytes(esc_buf))
                esc_buf.clear()
            return
        if ch in ("\n", "\r"):
            flush_line_with_suffix(ch.encode("utf-8"))
            return
        if ch == "\t":
            flush_line_with_suffix(b"\t")
            return
        if ch in ("\x08", "\x7f"):
            backspace_local()
            return
        if ch in ("\x03", "\x04", "\x1a"):
            flush_line_with_suffix(ch.encode("utf-8"))
            return
        if ch == "\x00":
            return
        if o >= 32 or o > 127:
            line_chars.append(ch)
            local_echo(ch)
            return
        flush_line_with_suffix(ch.encode("utf-8"))

    def process_stdin_chunk(data: bytes) -> None:
        text = utf8_dec.decode(data, final=False)
        for ch in text:
            handle_u_char(ch)

    try:
        with TerminalMode() as tm:
            in_fd = sys.stdin.fileno()
            while running:
                now = time.time()

                # Pull remote output before client->disk writes (resize/heartbeat). On slow NFS each
                # append to in.log can block seconds; otherwise the shell prompt appears very late or
                # feels "stuck" with a blank screen.
                while running:
                    events, out_offset = read_ndjson_incremental(session_dir / OUT_LOG_FILE, out_offset)
                    if not events:
                        break
                    for evt in events:
                        evt_type = evt.get("type")
                        if evt_type in ("stdout", "stderr"):
                            data_b64 = evt.get("data_b64", "")
                            if data_b64:
                                os.write(sys.stdout.fileno(), b64_decode(data_b64))
                        elif evt_type == "exit":
                            exit_code = int(evt.get("code", 0))
                            saw_exit = True
                            running = False

                if not running:
                    break

                if pending_resize:
                    send_resize()
                    pending_resize = False
                if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                    in_seq += 1
                    append_input(session_dir, in_seq, "heartbeat")
                    last_heartbeat = now

                if tm.enabled:
                    ready, _, _ = select.select([in_fd], [], [], CLIENT_POLL_INTERVAL)
                    if ready:
                        data = os.read(in_fd, 4096)
                        if data:
                            process_stdin_chunk(data)
                else:
                    data = os.read(in_fd, 4096) if select.select([in_fd], [], [], 0)[0] else b""
                    if not data:
                        time.sleep(CLIENT_POLL_INTERVAL)
                    else:
                        in_seq += 1
                        append_input(session_dir, in_seq, "stdin", data_b64=b64_encode(data))
            if line_chars:
                disk_stdin(line_bytes())
                line_chars.clear()
            if esc_buf:
                flush_line_with_suffix(bytes(esc_buf))
                esc_buf.clear()
    finally:
        signal.signal(signal.SIGWINCH, old_handler)

    if not saw_exit:
        print("\n[hpcsh-client] disconnected before exit event", file=sys.stderr)
    return exit_code


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Shared-disk offline HPC terminal client")
    parser.add_argument("--node", required=True, help="Node name, e.g. node01")
    parser.add_argument("--session", help="Reuse specific session id")
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Keep session directory on exit for debugging/reconnect",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    root = shared_root_from_env()
    node_dir = root / args.node
    node_dir.mkdir(parents=True, exist_ok=True)

    if args.session:
        session_id = args.session
    else:
        session_id = None
        for _ in range(64):
            sid = make_session_id()
            if not (node_dir / f"session_{sid}").exists():
                session_id = sid
                break
        if session_id is None:
            print("[hpcsh-client] could not allocate a free session id", file=sys.stderr)
            sys.exit(1)
    session_dir = node_dir / f"session_{session_id}"
    cleanup = not args.no_cleanup

    if not session_dir.exists():
        create_session(session_dir, args.node, cleanup=cleanup)
        print(f"[hpcsh-client] created session: {session_dir.name}")
    else:
        print(f"[hpcsh-client] reusing session: {session_dir.name}")

    print("[hpcsh-client] connected. type `exit` to quit remote shell.")
    sys.stdout.flush()
    sys.stderr.flush()
    code = 0
    try:
        code = run_interactive(session_dir)
    except KeyboardInterrupt:
        pass
    finally:
        append_ndjson(
            session_dir / EVENT_LOG_FILE,
            {"ts_ms": now_ms(), "level": "info", "message": "client closed", "client_pid": os.getpid()},
        )
        append_ndjson(session_dir / IN_LOG_FILE, {"seq": 999999999, "ts_ms": now_ms(), "type": "close"})
        if cleanup:
            time.sleep(0.2)
            try:
                shutil.rmtree(session_dir)
                print(f"[hpcsh-client] cleaned: {session_dir}")
            except OSError:
                print(f"[hpcsh-client] cleanup skipped: {session_dir}", file=sys.stderr)
    sys.exit(code)


if __name__ == "__main__":
    main()
