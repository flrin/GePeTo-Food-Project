"""Build, publish and load the search indexes (Chroma vectors + BM25 keywords).

    python -m gepeto.index build            # (re)build from the SQLite foods and recipes
    python -m gepeto.index status           # what is published, and is it up to date?

Layout (GEPETO_INDEX_DIR, default ./indexes, not committed):

    indexes/CURRENT          name of the published version, e.g. "v0003"
    indexes/v0003/chroma/    Chroma persistent collection "gepeto_chunks" (cosine)
    indexes/v0003/bm25.json  the same chunks, tokenized for BM25
    indexes/v0003/manifest.json  model, revision, chunk settings, data fingerprint, counts

A build writes a new version directory next to the published one. Only once
the Chroma collection and the BM25 file are written and read back correctly is
CURRENT switched to it (an atomic rename). Until then, and if the build fails,
readers keep using the previous version. Running servers notice the new
CURRENT on their next search. The previous version is kept; older ones are removed.

Rebuilds are incremental: a chunk whose text did not change reuses its vector
from the previous version, changed and new chunks are embedded, and chunks of
records that changed or disappeared are not carried over (superseded).
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .chunks import CHUNK_TOKENS, OVERLAP_TOKENS, Chunk, build_chunks, data_fingerprint
from .config import Settings
from .db import connect
from .embeddings import DIMENSIONS
from .retrieval import DEPTH, KEEP, RRF_K, bm25_tokens

COLLECTION = "gepeto_chunks"
CURRENT = "CURRENT"
KEEP_VERSIONS = 2
BM25_PARAMS = {"k1": 1.5, "b": 0.75}
VERSION_PATTERN = re.compile(r"^v(\d{4,})$")


class IndexBuildError(RuntimeError):
    pass


def _chroma(path: Path):
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    return chromadb.PersistentClient(path=str(path), settings=ChromaSettings(anonymized_telemetry=False))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def published_name(index_dir: Path) -> str | None:
    try:
        name = (index_dir / CURRENT).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return name if VERSION_PATTERN.match(name) else None


class IndexVersion:
    """One published, read-only version: the Chroma collection and the BM25 model."""

    def __init__(self, path: Path):
        from rank_bm25 import BM25Okapi
        self.path = path
        self.name = path.name
        self.manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        keywords = json.loads((path / "bm25.json").read_text(encoding="utf-8"))
        self.chunks: list[dict] = keywords["chunks"]
        self._text = {chunk["chunk_id"]: chunk["text"] for chunk in self.chunks}
        self.bm25 = BM25Okapi([chunk["tokens"] for chunk in self.chunks], **keywords["params"])
        self.collection = _chroma(path / "chroma").get_collection(COLLECTION)
        if self.collection.count() != len(self.chunks):
            raise IndexBuildError(f"{self.name}: Chroma has {self.collection.count()} chunks, BM25 has {len(self.chunks)}")

    def chunk_text(self, chunk_id: str) -> str:
        return self._text.get(chunk_id, "")

    def is_current_for(self, conn: sqlite3.Connection) -> bool:
        return self.manifest["data_fingerprint"] == data_fingerprint(conn)


def load_published(index_dir: Path) -> IndexVersion | None:
    name = published_name(index_dir)
    return None if name is None else IndexVersion(index_dir / name)


class LiveIndex:
    """The published version for one index directory, reloaded when CURRENT changes."""

    def __init__(self, index_dir: Path):
        self.index_dir = Path(index_dir)
        self._version: IndexVersion | None = None
        self._lock = threading.Lock()

    def current(self) -> IndexVersion | None:
        name = published_name(self.index_dir)
        with self._lock:
            if name is None:
                return None
            if self._version is None or self._version.name != name:
                self._version = IndexVersion(self.index_dir / name)
            return self._version


_live: dict[Path, LiveIndex] = {}


def live(index_dir: Path | str) -> LiveIndex:
    key = Path(index_dir).resolve()
    if key not in _live:
        _live[key] = LiveIndex(key)
    return _live[key]


@contextmanager
def _build_lock(index_dir: Path):
    index_dir.mkdir(parents=True, exist_ok=True)
    with open(index_dir / ".build.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _versions(index_dir: Path) -> list[str]:
    return sorted((p.name for p in index_dir.iterdir() if p.is_dir() and VERSION_PATTERN.match(p.name)),
                  key=lambda name: int(name[1:]))


def _previous_vectors(index_dir: Path, embedder) -> dict[str, np.ndarray]:
    """text hash -> vector from the published version, if it used the same model."""
    try:
        previous = load_published(index_dir)
    except Exception:  # noqa: BLE001 - a broken old version just means nothing to reuse
        return {}
    if previous is None or previous.manifest["model"] != {"name": embedder.name, "revision": embedder.revision}:
        return {}
    stored = previous.collection.get(include=["embeddings", "metadatas"])
    return {meta["text_hash"]: np.asarray(vector, dtype=np.float32)
            for meta, vector in zip(stored["metadatas"], stored["embeddings"])}


def _write_publish(index_dir: Path, name: str) -> None:
    tmp = index_dir / f".{CURRENT}.{os.getpid()}.tmp"
    tmp.write_text(name + "\n", encoding="utf-8")
    os.replace(tmp, index_dir / CURRENT)


def corpus_hash(chunks: list[Chunk]) -> str:
    digest = hashlib.sha256()
    for c in chunks:
        digest.update(f"{c.chunk_id}|{c.text_hash}|{json.dumps(c.chroma_metadata(), sort_keys=True)}\n".encode())
    return digest.hexdigest()


def build(conn: sqlite3.Connection, index_dir: Path | str, embedder=None, force: bool = False,
          _fail_before_publish: bool = False) -> dict:
    """Build a new version from SQLite and publish it. Returns its manifest.

    If the published version was built from exactly the same chunks with the same
    model, nothing is written and that manifest comes back with "unchanged": True."""
    if embedder is None:
        from .embeddings import default_embedder
        embedder = default_embedder()
    index_dir = Path(index_dir)
    with _build_lock(index_dir):
        chunks: list[Chunk] = build_chunks(conn, embedder)
        if not chunks:
            raise IndexBuildError("nothing to index: import foods and recipes first")
        ids = [c.chunk_id for c in chunks]
        if len(set(ids)) != len(ids):
            raise IndexBuildError("duplicate chunk ids")
        too_long = [c.chunk_id for c in chunks if c.tokens > CHUNK_TOKENS]
        if too_long:
            raise IndexBuildError(f"chunks over {CHUNK_TOKENS} tokens: {too_long[:5]}")

        previous_name = published_name(index_dir)
        corpus = corpus_hash(chunks)
        if previous_name and not force:
            try:
                previous = json.loads((index_dir / previous_name / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                previous = {}
            if previous.get("corpus_hash") == corpus and previous.get("model") == {
                    "name": embedder.name, "revision": embedder.revision}:
                return {**previous, "unchanged": True}
        reuse = _previous_vectors(index_dir, embedder)
        missing = [c for c in chunks if c.text_hash not in reuse]
        fresh = dict(zip((c.text_hash for c in missing), embedder.encode([c.text for c in missing])))
        vectors = np.stack([reuse.get(c.text_hash, fresh.get(c.text_hash)) for c in chunks])
        if vectors.shape != (len(chunks), DIMENSIONS):
            raise IndexBuildError(f"unexpected embedding shape {vectors.shape}")
        current_hashes = {c.text_hash for c in chunks}

        existing = _versions(index_dir)
        name = f"v{(int(existing[-1][1:]) + 1) if existing else 1:04d}"
        path = index_dir / name
        client = _chroma(path / "chroma")
        collection = client.create_collection(
            COLLECTION, embedding_function=None,
            configuration={"hnsw": {"space": "cosine", "ef_construction": 200, "ef_search": 200}},
        )
        for start in range(0, len(chunks), 1000):
            part = chunks[start:start + 1000]
            collection.add(ids=[c.chunk_id for c in part], embeddings=vectors[start:start + len(part)],
                           documents=[c.text for c in part], metadatas=[c.chroma_metadata() for c in part])
        keywords = {
            "params": BM25_PARAMS,
            "chunks": [{"chunk_id": c.chunk_id, "text": c.text, "tokens": bm25_tokens(c.text),
                        "metadata": c.chroma_metadata()} for c in chunks],
        }
        (path / "bm25.json").write_text(json.dumps(keywords, ensure_ascii=False), encoding="utf-8")
        kinds: dict[str, int] = {}
        for c in chunks:
            kinds[f"{c.kind}:{c.part}"] = kinds.get(f"{c.kind}:{c.part}", 0) + 1
        manifest = {
            "version": name,
            "created_at": _now(),
            "model": {"name": embedder.name, "revision": embedder.revision},
            "dimensions": DIMENSIONS,
            "distance": "cosine",
            "chunking": {"max_tokens": CHUNK_TOKENS, "overlap_tokens": OVERLAP_TOKENS,
                         "measured_with": "embedding tokenizer, title included, no special tokens"},
            "retrieval": {"dense_candidates": DEPTH, "bm25_candidates": DEPTH, "rrf_k": RRF_K, "keep": KEEP},
            "bm25": BM25_PARAMS,
            "data_fingerprint": data_fingerprint(conn),
            "corpus_hash": corpus,
            "records": len({c.record_id for c in chunks}),
            "chunks": len(chunks),
            "chunk_parts": kinds,
            "max_chunk_tokens": max(c.tokens for c in chunks),
            "embedded": len(missing),
            "reused": len(chunks) - len(missing),
            "superseded": len(set(reuse) - current_hashes),
            "previous_version": previous_name,
        }
        (path / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        del collection, client

        IndexVersion(path)  # read both indexes back before anyone can see this version
        if _fail_before_publish:
            raise IndexBuildError("simulated failure before publishing")
        _write_publish(index_dir, name)

        keep = {name, previous_name} if previous_name else {name}
        for old in _versions(index_dir):
            if old not in keep and int(old[1:]) < int(name[1:]):
                shutil.rmtree(index_dir / old, ignore_errors=True)
        return manifest


def status(conn: sqlite3.Connection, index_dir: Path) -> dict:
    version = load_published(index_dir)
    if version is None:
        return {"published": None, "up_to_date": False}
    return {"published": version.name, "up_to_date": version.is_current_for(conn),
            **{k: version.manifest[k] for k in ("created_at", "records", "chunks", "max_chunk_tokens", "model")}}


def main(argv: list[str] | None = None) -> None:
    settings = Settings.from_environment()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["build", "status"])
    parser.add_argument("--db", type=Path, default=settings.db_path)
    parser.add_argument("--index-dir", type=Path, default=settings.index_dir)
    parser.add_argument("--force", action="store_true", help="publish a new version even if nothing changed")
    args = parser.parse_args(argv)
    if not args.db.exists():
        sys.exit(f"no database at {args.db}: run the importer first")
    conn = connect(args.db)
    try:
        if args.command == "build":
            m = build(conn, args.index_dir, force=args.force)
            print(describe(m, args.index_dir))
        else:
            print(json.dumps(status(conn, args.index_dir), indent=2))
    finally:
        conn.close()


def describe(m: dict, index_dir: Path) -> str:
    if m.get("unchanged"):
        return f"index {m['version']} is already up to date ({m['chunks']} chunks), nothing published  ->  {index_dir}"
    return (f"published {m['version']}: {m['records']} records, {m['chunks']} chunks "
            f"(max {m['max_chunk_tokens']} tokens), embedded {m['embedded']}, reused {m['reused']}, "
            f"superseded {m['superseded']}  ->  {index_dir}")


if __name__ == "__main__":
    main()
