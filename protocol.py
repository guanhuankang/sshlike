import base64
import json
import os
import random
import string
import tempfile
import time
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


PROTOCOL_VERSION = 1

# Latency tuning (shared disk polling). Lower = snappier, slightly more CPU/NFS ops.
CLIENT_POLL_INTERVAL = 0.008
SERVER_SESSION_POLL_INTERVAL = 0.008
SERVER_SCAN_INTERVAL = 0.04

META_FILE = "meta.json"
STATE_FILE = "state.json"
IN_LOG_FILE = "in.log"
OUT_LOG_FILE = "out.log"
EVENT_LOG_FILE = "event.log"
LOCK_FILE = "lock"
PID_FILE = "pid"


def now_ms() -> int:
    return int(time.time() * 1000)


def b64_encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64_decode(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def random_suffix(length: int = 8) -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def make_session_id() -> str:
    """Short unique id for directory name session_<id> (8 lowercase alphanumeric chars)."""
    return random_suffix(8)


def atomic_write_json(path: Path, payload: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        json.dump(payload, tmp, ensure_ascii=True, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def read_json(path: Path, default: Dict) -> Dict:
    if not path.exists():
        return dict(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(default)


def append_ndjson(path: Path, event: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line)
        f.flush()
        # Per-line fsync is very slow on NFS; enable with HPCSH_FSYNC=1 if the other side
        # does not see new lines without a full sync on your storage.
        if os.environ.get("HPCSH_FSYNC", "").strip().lower() in ("1", "true", "yes"):
            os.fsync(f.fileno())


def read_ndjson_incremental(path: Path, offset: int) -> Tuple[List[Dict], int]:
    if not path.exists():
        return [], offset
    size = path.stat().st_size
    if offset > size:
        offset = 0
    with path.open("r", encoding="utf-8") as f:
        f.seek(offset)
        chunk = f.read()
        new_offset = f.tell()
    if chunk and not chunk.endswith("\n"):
        last_newline = chunk.rfind("\n")
        if last_newline == -1:
            return [], offset
        chunk = chunk[: last_newline + 1]
        new_offset = offset + len(chunk)
    lines = chunk.splitlines()
    events: List[Dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events, new_offset


def ensure_session_layout(session_dir: Path) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    for name in (IN_LOG_FILE, OUT_LOG_FILE, EVENT_LOG_FILE):
        p = session_dir / name
        if not p.exists():
            p.write_text("", encoding="utf-8")


def list_session_dirs(node_dir: Path) -> Iterable[Path]:
    if not node_dir.exists():
        return []
    return sorted([p for p in node_dir.iterdir() if p.is_dir() and p.name.startswith("session_")])


def shared_root_from_env() -> Path:
    """Shared root: $HPCSH_ROOT if set, else current working directory (login/server must agree)."""
    raw = (os.environ.get("HPCSH_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.cwd().resolve()
