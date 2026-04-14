#!/usr/bin/env python3

import argparse
import errno
import fcntl
import os
import pty
import select
import signal
import struct
import termios
import threading
import time
from pathlib import Path
from typing import Dict

from protocol import (
    EVENT_LOG_FILE,
    IN_LOG_FILE,
    LOCK_FILE,
    META_FILE,
    OUT_LOG_FILE,
    PID_FILE,
    PROTOCOL_VERSION,
    STATE_FILE,
    append_ndjson,
    atomic_write_json,
    b64_decode,
    b64_encode,
    ensure_session_layout,
    list_session_dirs,
    now_ms,
    read_json,
    read_ndjson_incremental,
    shared_root_from_env,
)


POLL_INTERVAL = 0.05
HEARTBEAT_INTERVAL = 2.0


def set_nonblocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def apply_resize(fd: int, rows: int, cols: int) -> None:
    if rows <= 0 or cols <= 0:
        return
    winsz = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsz)


class SessionRunner(threading.Thread):
    def __init__(self, session_dir: Path):
        super().__init__(daemon=True)
        self.session_dir = session_dir
        self.stop_event = threading.Event()
        self.in_offset = 0
        self.out_seq = 0
        self.last_state_heartbeat = 0.0
        self.last_client_heartbeat_ms = now_ms()
        self.master_fd = -1
        self.child_pid = -1

    @property
    def state_path(self) -> Path:
        return self.session_dir / STATE_FILE

    def _write_state(self, state: str, extra: Dict = None) -> None:
        payload = read_json(self.state_path, default={})
        payload.update(
            {
                "protocol": PROTOCOL_VERSION,
                "state": state,
                "server_pid": os.getpid(),
                "session": self.session_dir.name,
                "updated_ms": now_ms(),
                "client_heartbeat_ms": self.last_client_heartbeat_ms,
            }
        )
        if self.child_pid > 0:
            payload["shell_pid"] = self.child_pid
        if extra:
            payload.update(extra)
        atomic_write_json(self.state_path, payload)

    def _append_out(self, event_type: str, data: bytes = b"", extra: Dict = None) -> None:
        self.out_seq += 1
        payload = {
            "seq": self.out_seq,
            "ts_ms": now_ms(),
            "type": event_type,
        }
        if data:
            payload["data_b64"] = b64_encode(data)
        if extra:
            payload.update(extra)
        append_ndjson(self.session_dir / OUT_LOG_FILE, payload)

    def _append_event(self, level: str, message: str, extra: Dict = None) -> None:
        payload = {"ts_ms": now_ms(), "level": level, "message": message}
        if extra:
            payload.update(extra)
        append_ndjson(self.session_dir / EVENT_LOG_FILE, payload)

    def _spawn_shell(self) -> None:
        shell = os.environ.get("SHELL", "/bin/bash")
        pid, fd = pty.fork()
        if pid == 0:
            os.execv(shell, [shell, "-i"])
        self.child_pid = pid
        self.master_fd = fd
        set_nonblocking(self.master_fd)
        (self.session_dir / PID_FILE).write_text(f"{self.child_pid}\n", encoding="utf-8")
        self._append_event("info", "shell started", {"shell": shell, "shell_pid": self.child_pid})

    def _handle_input_events(self) -> None:
        events, self.in_offset = read_ndjson_incremental(self.session_dir / IN_LOG_FILE, self.in_offset)
        for evt in events:
            evt_type = evt.get("type")
            if evt_type == "stdin":
                data_b64 = evt.get("data_b64", "")
                if data_b64 and self.master_fd >= 0:
                    os.write(self.master_fd, b64_decode(data_b64))
            elif evt_type == "resize":
                rows = int(evt.get("rows", 0))
                cols = int(evt.get("cols", 0))
                if self.master_fd >= 0:
                    apply_resize(self.master_fd, rows, cols)
            elif evt_type == "signal":
                if self.child_pid > 0:
                    sig_name = str(evt.get("name", "SIGINT"))
                    sig = getattr(signal, sig_name, signal.SIGINT)
                    os.kill(self.child_pid, sig)
            elif evt_type == "heartbeat":
                self.last_client_heartbeat_ms = now_ms()
            elif evt_type == "close":
                self.stop_event.set()

    def _pump_output(self) -> None:
        if self.master_fd < 0:
            return
        ready, _, _ = select.select([self.master_fd], [], [], 0)
        if not ready:
            return
        try:
            data = os.read(self.master_fd, 65536)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            raise
        if data:
            self._append_out("stdout", data=data)

    def _poll_child_exit(self):
        if self.child_pid <= 0:
            return None
        pid, status = os.waitpid(self.child_pid, os.WNOHANG)
        if pid == 0:
            return None
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return 128 + os.WTERMSIG(status)
        return 255

    def run(self) -> None:
        ensure_session_layout(self.session_dir)
        meta = read_json(self.session_dir / META_FILE, default={})
        self._write_state("running", {"meta_user": meta.get("user", "unknown")})
        try:
            self._spawn_shell()
            rows = int(meta.get("rows", 24))
            cols = int(meta.get("cols", 80))
            if self.master_fd >= 0:
                apply_resize(self.master_fd, rows, cols)
            while not self.stop_event.is_set():
                self._handle_input_events()
                self._pump_output()
                exit_code = self._poll_child_exit()
                now = time.time()
                if now - self.last_state_heartbeat >= HEARTBEAT_INTERVAL:
                    self._write_state("running")
                    self.last_state_heartbeat = now
                if exit_code is not None:
                    self._append_out("exit", extra={"code": exit_code})
                    self._write_state("exit", {"exit_code": exit_code})
                    self._append_event("info", "shell exited", {"code": exit_code})
                    return
                time.sleep(POLL_INTERVAL)
            if self.child_pid > 0:
                os.kill(self.child_pid, signal.SIGTERM)
            self._append_out("exit", extra={"code": 0, "reason": "closed"})
            self._write_state("exit", {"exit_code": 0, "reason": "closed"})
        except Exception as e:  # pragma: no cover - defensive logging path
            self._append_event("error", "session crashed", {"error": str(e)})
            self._append_out("exit", extra={"code": 1, "reason": "server_error"})
            self._write_state("exit", {"exit_code": 1, "reason": "server_error"})
        finally:
            if self.master_fd >= 0:
                try:
                    os.close(self.master_fd)
                except OSError:
                    pass
            lock_path = self.session_dir / LOCK_FILE
            if lock_path.exists():
                try:
                    lock_path.unlink()
                except OSError:
                    pass


def try_acquire_lock(session_dir: Path) -> bool:
    lock_path = session_dir / LOCK_FILE
    payload = f"pid={os.getpid()} ts_ms={now_ms()}\n"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(lock_path), flags)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    return True


def serve(node_dir: Path) -> None:
    node_dir.mkdir(parents=True, exist_ok=True)
    runners: Dict[str, SessionRunner] = {}
    print(f"[hpcsh-server] watching: {node_dir}")
    while True:
        for session_dir in list_session_dirs(node_dir):
            key = str(session_dir)
            runner = runners.get(key)
            if runner and runner.is_alive():
                continue
            state = read_json(session_dir / STATE_FILE, default={})
            if state.get("state") == "exit":
                continue
            ensure_session_layout(session_dir)
            if not try_acquire_lock(session_dir):
                continue
            new_runner = SessionRunner(session_dir)
            new_runner.start()
            runners[key] = new_runner
        time.sleep(0.2)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Shared-disk offline HPC terminal server")
    parser.add_argument("--node", required=True, help="Node name, e.g. node01")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    node_dir = shared_root_from_env() / args.node
    serve(node_dir)


if __name__ == "__main__":
    main()
