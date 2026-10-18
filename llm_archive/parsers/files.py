"""Stream plain and compressed transcripts without modifying the agent's files."""
from contextlib import contextmanager
from io import TextIOWrapper
from pathlib import Path


def transcript_files(root: Path):
    for path in root.rglob("*.jsonl*"):
        if path.name.endswith((".jsonl", ".jsonl.zst")):
            yield path


@contextmanager
def open_transcript(path: Path):
    if not path.name.endswith(".zst"):
        with path.open(encoding="utf-8", errors="replace") as stream:
            yield stream
        return
    try:
        import zstandard
    except ImportError:
        raise OSError("Compressed transcripts require zstandard. Reinstall: pip install -e .") from None
    try:
        with path.open("rb") as raw, zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True) as reader:
            with TextIOWrapper(reader, encoding="utf-8", errors="replace") as stream:
                yield stream
    except zstandard.ZstdError as exc:
        raise OSError(f"Could not decompress {path.name}: {exc}") from exc
