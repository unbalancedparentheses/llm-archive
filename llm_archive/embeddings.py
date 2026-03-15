"""Semantic embedding and search using ollama or sentence-transformers."""
import json
import subprocess
from pathlib import Path

import numpy as np

from llm_archive import db

EMBEDDINGS_DIR = Path.home() / ".local" / "share" / "llm-archive"
EMBEDDINGS_PATH = EMBEDDINGS_DIR / "embeddings.npz"
META_PATH = EMBEDDINGS_DIR / "embeddings_meta.json"

# Minimum message length to embed
MIN_CHARS = 20


def _ollama_available() -> bool:
    try:
        result = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0 and "nomic-embed-text" in result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _sentence_transformers_available() -> bool:
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


def _embed_ollama(texts: list[str], batch_size: int = 256) -> np.ndarray:
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        payload = json.dumps({"model": "nomic-embed-text", "input": batch})
        result = subprocess.run(
            ["curl", "-s", "http://localhost:11434/api/embed", "-d", payload],
            capture_output=True, text=True, timeout=120,
        )
        data = json.loads(result.stdout)
        all_embeddings.extend(data["embeddings"])
    return np.array(all_embeddings, dtype=np.float32)


def _embed_sentence_transformers(texts: list[str]) -> np.ndarray:
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("all-MiniLM-L6-v2")
    return model.encode(texts, show_progress_bar=False, convert_to_numpy=True).astype(np.float32)


def get_provider() -> tuple[str, int]:
    """Return (provider_name, embedding_dimension)."""
    if _ollama_available():
        return "ollama", 768
    if _sentence_transformers_available():
        return "sentence-transformers", 384
    raise RuntimeError(
        "No embedding provider available. "
        "Install nomic-embed-text via 'ollama pull nomic-embed-text' "
        "or install sentence-transformers: pip install sentence-transformers"
    )


def _embed(texts: list[str], provider: str) -> np.ndarray:
    if provider == "ollama":
        return _embed_ollama(texts)
    else:
        return _embed_sentence_transformers(texts)


def _load_existing() -> tuple[np.ndarray | None, np.ndarray | None, dict | None]:
    """Load existing embeddings. Returns (ids, embeddings, meta) or (None, None, None)."""
    if not EMBEDDINGS_PATH.exists() or not META_PATH.exists():
        return None, None, None
    data = np.load(EMBEDDINGS_PATH)
    meta = json.loads(META_PATH.read_text())
    return data["ids"], data["embeddings"], meta


def build_embeddings(conn, rebuild=False, progress_fn=None) -> tuple[int, int]:
    """Build or update embedding index. Returns (new_count, total_count)."""
    provider, dim = get_provider()

    existing_ids, existing_emb, meta = (None, None, None) if rebuild else _load_existing() or (None, None, None)

    # Check dimension mismatch
    if meta and meta.get("dimension") != dim:
        if not rebuild:
            raise RuntimeError(
                f"Embedding dimension mismatch: stored={meta['dimension']}, "
                f"current provider={provider} ({dim}). Run: llm-archive embed --rebuild"
            )

    all_ids = set(db.all_message_ids(conn))
    embedded_ids = set(existing_ids.tolist()) if existing_ids is not None else set()
    new_ids = sorted(all_ids - embedded_ids)

    if not new_ids:
        total = len(embedded_ids)
        return 0, total

    # Fetch texts for new IDs
    id_text_pairs = db.get_message_texts(conn, new_ids)

    # Filter short/noise messages
    filtered = [
        (mid, text[:2000]) for mid, text in id_text_pairs
        if text and len(text) >= MIN_CHARS
    ]

    if not filtered:
        total = len(embedded_ids)
        return 0, total

    filt_ids, filt_texts = zip(*filtered)

    # Embed in chunks with progress
    chunk_size = 256
    new_embeddings = []
    for i in range(0, len(filt_texts), chunk_size):
        chunk = list(filt_texts[i:i + chunk_size])
        new_embeddings.append(_embed(chunk, provider))
        if progress_fn:
            progress_fn(min(i + chunk_size, len(filt_texts)), len(filt_texts))

    new_emb = np.vstack(new_embeddings)
    new_id_arr = np.array(filt_ids, dtype=np.int64)

    # Merge with existing
    if existing_ids is not None and existing_emb is not None:
        final_ids = np.concatenate([existing_ids, new_id_arr])
        final_emb = np.vstack([existing_emb, new_emb])
    else:
        final_ids = new_id_arr
        final_emb = new_emb

    # Save
    EMBEDDINGS_DIR.mkdir(parents=True, exist_ok=True)
    np.savez(EMBEDDINGS_PATH, ids=final_ids, embeddings=final_emb)
    META_PATH.write_text(json.dumps({
        "provider": provider,
        "dimension": dim,
        "count": len(final_ids),
    }))

    return len(new_id_arr), len(final_ids)


def semantic_search(
    conn,
    query: str,
    project: str | None = None,
    source: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search by semantic similarity. Returns results with score."""
    existing_ids, existing_emb, meta = _load_existing()
    if existing_ids is None or meta is None:
        raise RuntimeError("No embeddings found. Run: llm-archive embed")

    provider = meta["provider"]
    # Verify provider is still available
    if provider == "ollama" and not _ollama_available():
        if _sentence_transformers_available():
            raise RuntimeError(
                "Embeddings were built with ollama but it's not available. "
                "Run: llm-archive embed --rebuild"
            )
        raise RuntimeError("Embedding provider (ollama) not available.")
    if provider == "sentence-transformers" and not _sentence_transformers_available():
        if _ollama_available():
            raise RuntimeError(
                "Embeddings were built with sentence-transformers but it's not installed. "
                "Run: llm-archive embed --rebuild"
            )
        raise RuntimeError("Embedding provider (sentence-transformers) not available.")

    # Embed query
    query_emb = _embed([query], provider)[0]

    # Cosine similarity
    norms = np.linalg.norm(existing_emb, axis=1)
    query_norm = np.linalg.norm(query_emb)
    # Avoid division by zero
    safe_norms = np.where(norms > 0, norms, 1.0)
    similarities = existing_emb @ query_emb / (safe_norms * query_norm)

    # Top K
    top_k = min(limit * 3, len(similarities))  # fetch extra, filter later
    top_indices = np.argsort(similarities)[-top_k:][::-1]
    top_ids = existing_ids[top_indices].tolist()
    top_scores = similarities[top_indices].tolist()

    # Fetch from DB with filters
    messages = db.get_messages_by_ids(conn, top_ids, project=project, source=source)
    msg_map = {m["id"]: m for m in messages}

    results = []
    for mid, score in zip(top_ids, top_scores):
        if mid in msg_map:
            m = msg_map[mid]
            snippet = (m["content"] or "")[:200].replace("\n", " ")
            results.append({
                "source": m["source"],
                "project": m["project"],
                "git_branch": m["git_branch"],
                "role": m["role"],
                "timestamp": m["timestamp"],
                "snippet": snippet,
                "score": round(score, 3),
            })
            if len(results) >= limit:
                break

    return results
