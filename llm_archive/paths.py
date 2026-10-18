"""Local state shared by the collector, journal, and CLI."""
import json
import os
import socket
import uuid
from contextlib import contextmanager
from pathlib import Path


def data_dir() -> Path:
    return Path(os.environ.get("LLM_ARCHIVE_HOME", Path.home() / ".local/share/llm-archive")).expanduser()


def device_id() -> str:
    root = data_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "device-id"
    try:
        with path.open("x") as f:
            f.write(str(uuid.uuid4()))
    except FileExistsError:
        pass
    return path.read_text().strip()


def machine_name() -> str:
    return socket.gethostname()


def read_config() -> dict:
    path = data_dir() / "config.json"
    return json.loads(path.read_text()) if path.exists() else {}


def atomic_write(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_bytes(path: Path, content: bytes) -> None:
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as f:
        temp = Path(f.name)
        try:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


@contextmanager
def collector_lock():
    # Also serializes manual ingest with scheduled checkpoints and sync imports.
    import fcntl
    root = data_dir()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "collector.lock").open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another llm-archive checkpoint is already running.") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
