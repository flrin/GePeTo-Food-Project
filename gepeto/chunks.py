"""Searchable text cards built from the SQLite records (Milestone 2, section 4).

* One card per food. Different foods never share a chunk.
* A recipe that fits in CHUNK_TOKENS stays one chunk. A longer one is split:
  the ingredient list stays together in one chunk, and the steps are split at
  step boundaries with about OVERLAP_TOKENS of overlap. If even the ingredient
  list is too long, that chunk becomes a short summary; the full list is always
  loaded from SQLite by recipe id before anything is calculated or checked.
* Every chunk starts with its title, and the limit includes the title.

Token counts come from the embedding model's tokenizer (see embeddings.py).
Chunks are only search keys: results are mapped back to their source record
(`record_id`) and the numbers shown to the user always come from SQLite.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field

from .nutrition import recipe_per_serving, rounded
from .search import RECIPE_DIET_TAGS, recipe_ingredients, source_ref

CHUNK_TOKENS = 200
OVERLAP_TOKENS = 30


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    record_id: str          # usda:171077 / recipe:r08 - what Recall@5 is measured on
    kind: str               # food / recipe
    part: str               # card / full / ingredients / summary / steps
    text: str
    tokens: int
    metadata: dict = field(default_factory=dict)

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def chroma_metadata(self) -> dict:
        return {"record_id": self.record_id, "kind": self.kind, "part": self.part,
                "text_hash": self.text_hash, **self.metadata}


def nutrient_line(values: dict) -> str:
    """'120 kcal, 22.5 g protein, 0 g carbohydrate, 2.6 g fat' - unknown values say so."""
    parts = []
    for key, label, unit in (("kcal", "kcal", ""), ("protein_g", "protein", " g"),
                             ("carbs_g", "carbohydrate", " g"), ("fat_g", "fat", " g")):
        value = values.get(key)
        parts.append(f"{label} unknown" if value is None else f"{value:g}{unit} {label}")
    return ", ".join(parts)


def _fit(text: str, budget: int, embedder) -> str:
    """Cut `text` to `budget` tokens. Only used for oversized user-entered names."""
    ids = embedder.token_ids(text)
    return text if len(ids) <= budget else embedder.decode(ids[:budget])


def food_chunk(row: sqlite3.Row, embedder) -> Chunk:
    record_id = source_ref(row["source"], row["source_id"])
    category = row["category"] or "unknown category"
    body = (f"\nCategory: {category}. Preparation: {row['prep_state']}."
            f"\nPer 100 g: {nutrient_line(dict(row))}.")
    title = _fit(row["description"], CHUNK_TOKENS - embedder.count_tokens(body), embedder)
    text = title + body
    return Chunk(
        chunk_id=record_id, record_id=record_id, kind="food", part="card", text=text,
        tokens=embedder.count_tokens(text),
        metadata={"title": row["description"], "prep_state": row["prep_state"], "category": category,
                  "user_entered": bool(row["user_entered"])},
    )


def _step_windows(prefix: str, steps: list[str], embedder) -> list[str]:
    """Pack whole steps into windows of at most CHUNK_TOKENS (prefix included).

    Each window after the first starts with the last OVERLAP_TOKENS of the previous
    one, so a sentence near a boundary can be found from either side. A single step
    longer than a window is cut by tokens, with the same overlap.
    """
    budget = CHUNK_TOKENS - embedder.count_tokens(prefix)
    pieces: list[list[int]] = []
    for step in steps:
        ids = embedder.token_ids(step)
        stride = budget - OVERLAP_TOKENS - len(embedder.token_ids("..."))  # room for the overlap
        if len(ids) <= stride:
            pieces.append(ids)
        else:
            pieces.extend(ids[start:start + stride] for start in range(0, len(ids), stride))
    windows: list[list[int]] = []
    current: list[int] = []
    for ids in pieces:
        if current and len(current) + len(ids) > budget:
            windows.append(current)
            current = embedder.token_ids("...") + current[-OVERLAP_TOKENS:]
        current = current + ids
    if current:
        windows.append(current)
    return [prefix + embedder.decode(ids) for ids in windows]


def recipe_chunks(conn: sqlite3.Connection, row: sqlite3.Row, embedder) -> list[Chunk]:
    recipe_id, title = row["id"], row["title"]
    record_id = f"recipe:{recipe_id}"
    tags = json.loads(row["tags"])
    steps = [f"{n}. {step}" for n, step in enumerate(json.loads(row["steps"]), start=1)]
    ingredients = recipe_ingredients(conn, recipe_id)
    per_serving = rounded({k: v for k, v in recipe_per_serving(conn, recipe_id).items() if k != "estimate"})

    head = f"{title} (recipe {recipe_id})"
    about = (f"\n{row['meal_type'].capitalize()} recipe. Tags: {', '.join(tags) or 'none'}. "
             f"{row['servings']} serving{'s' if row['servings'] != 1 else ''}, {row['weight_state']} ingredient weights."
             f"\nEstimated per serving: {nutrient_line(per_serving)}.")
    ingredient_line = "\nIngredients: " + ", ".join(f"{i['grams']:g} g {i['name']}" for i in ingredients) + "."
    steps_line = "\nSteps: " + " ".join(steps)

    metadata = {"title": title, "recipe_id": recipe_id, "meal_type": row["meal_type"],
                **{diet: bool(set(tags) & allowed) for diet, allowed in RECIPE_DIET_TAGS.items()}}

    def chunk(n: int, part: str, text: str) -> Chunk:
        return Chunk(chunk_id=f"{record_id}#{n}", record_id=record_id, kind="recipe", part=part,
                     text=text, tokens=embedder.count_tokens(text), metadata=metadata)

    whole = head + about + ingredient_line + steps_line
    if embedder.count_tokens(whole) <= CHUNK_TOKENS:
        return [chunk(0, "full", whole)]

    first = head + about + ingredient_line
    part = "ingredients"
    if embedder.count_tokens(first) > CHUNK_TOKENS:
        # Too long to keep whole: a summary that names the ingredients, expanded by id later.
        names = [i["name"] for i in ingredients]
        part = "summary"
        while True:
            hidden = len(ingredients) - len(names)
            more = f" and {hidden} more" if hidden else ""
            first = head + about + "\nIngredients (full list loaded by recipe id): " + ", ".join(names) + more + "."
            if embedder.count_tokens(first) <= CHUNK_TOKENS or not names:
                break
            names.pop()
    chunks = [chunk(0, part, first)]
    for n, text in enumerate(_step_windows(f"{head}, steps:\n", steps, embedder), start=1):
        chunks.append(chunk(n, "steps", text))
    return chunks


def build_chunks(conn: sqlite3.Connection, embedder) -> list[Chunk]:
    chunks = [food_chunk(row, embedder) for row in conn.execute("SELECT * FROM foods ORDER BY source, source_id")]
    for row in conn.execute("SELECT * FROM recipes ORDER BY id"):
        chunks.extend(recipe_chunks(conn, row, embedder))
    return chunks


def data_fingerprint(conn: sqlite3.Connection) -> str:
    """Changes whenever a food or recipe the index was built from changes."""
    digest = hashlib.sha256()
    for row in conn.execute("SELECT source, source_id, content_hash FROM foods ORDER BY source, source_id"):
        digest.update(f"f|{row[0]}|{row[1]}|{row[2]}\n".encode())
    for row in conn.execute("SELECT id, content_hash FROM recipes ORDER BY id"):
        digest.update(f"r|{row[0]}|{row[1]}\n".encode())
    return digest.hexdigest()
