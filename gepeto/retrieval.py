"""Hybrid retrieval: Chroma (dense) + BM25 (keywords), merged by reciprocal rank fusion.

Milestone 2, section 4: ten dense and ten BM25 candidates, merged with
`1 / (60 + rank)` per list (ranks start at 1), five unique results kept.

Both lists are first collapsed to source records (a recipe split into several
chunks counts once per list, at its best chunk's rank), then fused. Ties are
broken by the dense rank, then the BM25 rank, then the record id, so the same
query on the same index always gives the same order.

Filters are applied inside both retrievers: equality on chunk metadata, and an
optional allow-list of record ids. The caller computes that allow-list from the
full SQL records (diet, excluded ingredients, kcal and protein limits), so a
hard rule never depends on similarity and an eligible record is never pushed
out of the top ten by an ineligible one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DEPTH = 10      # candidates per retriever
KEEP = 5        # unique records after fusion
RRF_K = 60
METHODS = ("hybrid", "dense", "bm25")

# A small, standard English stop list (no food words).
BM25_STOP_WORDS = frozenset("""
a an and are as at be but by can could do does for from g gram grams had has have how i if in into is it
its me much my of on or per please so some than that the their them then there these this to was what
when which with would you your
""".split())


def stem(word: str) -> str:
    """Plural folding, applied to the index and the query alike: berries/berry, eggs/egg."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("oes"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def bm25_tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [stem(w) for w in words if w not in BM25_STOP_WORDS and not w.isdigit() and len(w) > 1]


def collapse(ranked_chunks: list[tuple[str, str, float]]) -> list[tuple[str, float, list[str]]]:
    """[(chunk_id, record_id, score)] best first -> [(record_id, best score, chunk ids)] best first."""
    order: dict[str, tuple[float, list[str]]] = {}
    for chunk_id, record_id, score in ranked_chunks:
        if record_id in order:
            order[record_id][1].append(chunk_id)
        else:
            order[record_id] = (score, [chunk_id])
    return [(record_id, score, chunk_ids) for record_id, (score, chunk_ids) in order.items()]


def rrf(rankings: list[list[str]], k: int = RRF_K) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, record_id in enumerate(ranking, start=1):
            scores[record_id] = scores.get(record_id, 0.0) + 1.0 / (k + rank)
    return scores


@dataclass
class Hit:
    record_id: str
    score: float
    dense_rank: int | None = None
    bm25_rank: int | None = None
    similarity: float | None = None     # cosine similarity of the best dense chunk
    bm25_score: float | None = None
    matched_terms: list[str] = field(default_factory=list)
    chunk_ids: list[str] = field(default_factory=list)

    def evidence(self, method: str) -> dict:
        return {
            "method": method, "score": round(self.score, 6),
            "dense_rank": self.dense_rank, "bm25_rank": self.bm25_rank,
            "similarity": None if self.similarity is None else round(self.similarity, 4),
            "bm25_score": None if self.bm25_score is None else round(self.bm25_score, 4),
            "matched_terms": self.matched_terms, "chunk_ids": self.chunk_ids,
        }


@dataclass(frozen=True)
class Filters:
    kind: str                                  # food / recipe
    equals: dict = field(default_factory=dict)  # metadata field -> value
    record_ids: frozenset[str] | None = None    # allow-list; None = no limit

    def chroma_where(self) -> dict:
        clauses = [{"kind": self.kind}] + [{key: value} for key, value in sorted(self.equals.items())]
        if self.record_ids is not None:
            clauses.append({"record_id": {"$in": sorted(self.record_ids)}})
        return clauses[0] if len(clauses) == 1 else {"$and": clauses}

    def allows(self, metadata: dict) -> bool:
        if metadata["kind"] != self.kind:
            return False
        if any(metadata.get(key) != value for key, value in self.equals.items()):
            return False
        return self.record_ids is None or metadata["record_id"] in self.record_ids


class HybridRetriever:
    def __init__(self, version, embedder):
        self.version, self.embedder = version, embedder

    def dense(self, query: str, filters: Filters, depth: int = DEPTH) -> list[tuple[str, str, float]]:
        if filters.record_ids is not None and not filters.record_ids:
            return []
        vector = self.embedder.encode([query])
        found = self.version.collection.query(
            query_embeddings=vector, n_results=depth, where=filters.chroma_where(),
            include=["metadatas", "distances"],
        )
        return [(chunk_id, meta["record_id"], 1.0 - distance)
                for chunk_id, meta, distance in zip(found["ids"][0], found["metadatas"][0], found["distances"][0])]

    def keyword(self, query: str, filters: Filters, depth: int = DEPTH) -> list[tuple[str, str, float]]:
        tokens = bm25_tokens(query)
        if not tokens:
            return []
        scores = self.version.bm25.get_scores(tokens)
        candidates = [
            (float(scores[i]), chunk["chunk_id"], chunk["metadata"]["record_id"])
            for i, chunk in enumerate(self.version.chunks)
            if scores[i] > 0 and filters.allows(chunk["metadata"])
        ]
        candidates.sort(key=lambda c: (-c[0], c[1]))
        return [(chunk_id, record_id, score) for score, chunk_id, record_id in candidates[:depth]]

    def search(self, query: str, filters: Filters, keep: int = KEEP, depth: int = DEPTH,
               method: str = "hybrid") -> list[Hit]:
        if method not in METHODS:
            raise ValueError(f"unknown retrieval method {method!r}")
        dense = collapse(self.dense(query, filters, depth)) if method != "bm25" else []
        keyword = collapse(self.keyword(query, filters, depth)) if method != "dense" else []
        dense_rank = {record_id: n for n, (record_id, _, _) in enumerate(dense, start=1)}
        bm25_rank = {record_id: n for n, (record_id, _, _) in enumerate(keyword, start=1)}
        scores = rrf([[r for r, _, _ in dense], [r for r, _, _ in keyword]])
        never = len(dense) + len(keyword) + 1
        order = sorted(scores, key=lambda r: (-scores[r], dense_rank.get(r, never), bm25_rank.get(r, never), r))

        query_tokens = set(bm25_tokens(query))
        dense_info = {r: (s, c) for r, s, c in dense}
        bm25_info = {r: (s, c) for r, s, c in keyword}
        hits = []
        for record_id in order[:keep]:
            chunk_ids = list(dict.fromkeys(dense_info.get(record_id, (0, []))[1] + bm25_info.get(record_id, (0, []))[1]))
            texts = [self.version.chunk_text(chunk_id) for chunk_id in chunk_ids]
            matched = sorted(query_tokens & {t for text in texts for t in bm25_tokens(text)})
            hits.append(Hit(
                record_id=record_id, score=scores[record_id],
                dense_rank=dense_rank.get(record_id), bm25_rank=bm25_rank.get(record_id),
                similarity=dense_info[record_id][0] if record_id in dense_info else None,
                bm25_score=bm25_info[record_id][0] if record_id in bm25_info else None,
                matched_terms=matched, chunk_ids=chunk_ids,
            ))
        return hits
