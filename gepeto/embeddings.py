"""The embedding model and its tokenizer (Milestone 2, section 4).

sentence-transformers/all-MiniLM-L6-v2, pinned to one Hub revision so every
rebuild embeds with the same weights. Vectors are L2-normalized and stored in
Chroma with cosine distance. Chunk sizes are measured with this model's own
tokenizer, so "200 tokens" means 200 of the word pieces the model will see.

The model is downloaded once into the Hugging Face cache (~90 MB). Set
HF_HUB_OFFLINE=1 afterwards to make sure nothing is fetched again.
"""

from __future__ import annotations

import threading

import numpy as np

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
DIMENSIONS = 384
MODEL_MAX_TOKENS = 256  # the model truncates longer inputs; our chunks stay under CHUNK_TOKENS


class Embedder:
    """Lazy wrapper: the tokenizer loads in well under a second, the model only when needed."""

    name = MODEL_NAME
    revision = MODEL_REVISION

    def __init__(self):
        self._lock = threading.Lock()  # the MCP server warms the model up in a background thread
        self._loaded: dict = {}

    def _once(self, key: str, load):
        if key not in self._loaded:
            with self._lock:
                if key not in self._loaded:
                    self._loaded[key] = load()
        return self._loaded[key]

    @property
    def tokenizer(self):
        from transformers import AutoTokenizer
        return self._once("tokenizer", lambda: AutoTokenizer.from_pretrained(MODEL_NAME, revision=MODEL_REVISION))

    @property
    def model(self):
        from sentence_transformers import SentenceTransformer
        return self._once("model", lambda: SentenceTransformer(MODEL_NAME, revision=MODEL_REVISION, device="cpu"))

    def token_ids(self, text: str) -> list[int]:
        """Word pieces of `text`, without the [CLS]/[SEP] markers the model adds."""
        return self.tokenizer(text, add_special_tokens=False, verbose=False)["input_ids"]

    def count_tokens(self, text: str) -> int:
        return len(self.token_ids(text))

    def decode(self, ids: list[int]) -> str:
        return self.tokenizer.decode(ids)

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, DIMENSIONS), dtype=np.float32)
        vectors = self.model.encode(texts, batch_size=64, normalize_embeddings=True,
                                    convert_to_numpy=True, show_progress_bar=False)
        return vectors.astype(np.float32)


_shared: Embedder | None = None


def default_embedder() -> Embedder:
    """One embedder per process, so the model is loaded at most once."""
    global _shared
    if _shared is None:
        _shared = Embedder()
    return _shared
