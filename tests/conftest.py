import hashlib
import json
import re
import shutil
from pathlib import Path

import numpy as np
import pytest

from gepeto.config import DATA_DIR, EVALUATION_DIR
from gepeto.confirm import Confirmations
from gepeto.db import connect
from gepeto.importer import food_id_for, read_food_json, upsert_foods, upsert_recipes
from gepeto.profiles import save_profile
from gepeto.tools import ToolContext

SECRET = "test-secret"
DAY = "2026-11-18"


@pytest.fixture(scope="session")
def _seeded_db(tmp_path_factory) -> Path:
    """The 300 foods and 30 recipes, imported once and copied for each test."""
    path = tmp_path_factory.mktemp("seed") / "seed.db"
    conn = connect(path)
    upsert_foods(conn, read_food_json(DATA_DIR / "foods_300.jsonl"))
    upsert_recipes(conn, json.loads((DATA_DIR / "recipes.json").read_text(encoding="utf-8")))
    conn.close()
    return path


class FakeEmbedder:
    """The real tokenizer (so chunk sizes are real) with cheap hashed bag-of-words vectors.
    Used where only the build/publish mechanics are tested, not ranking quality."""

    name, revision = "fake-hash-embedder", "test"

    def __init__(self):
        from gepeto.embeddings import default_embedder
        self._real = default_embedder()
        self.encoded = 0

    def token_ids(self, text):
        return self._real.token_ids(text)

    def count_tokens(self, text):
        return self._real.count_tokens(text)

    def decode(self, ids):
        return self._real.decode(ids)

    def encode(self, texts):
        self.encoded += len(texts)
        out = np.zeros((len(texts), 384), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in re.findall(r"[a-z]+", text.lower()):
                out[row, int(hashlib.md5(word.encode()).hexdigest(), 16) % 384] += 1
            out[row] /= max(np.linalg.norm(out[row]), 1e-9)
        return out


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture(scope="session")
def index_dir(_seeded_db, tmp_path_factory) -> Path:
    """The real index (all-MiniLM-L6-v2) over the seeded foods and recipes, built once."""
    from gepeto.index import build
    path = tmp_path_factory.mktemp("indexes")
    conn = connect(_seeded_db)
    build(conn, path)
    conn.close()
    return path


@pytest.fixture
def db_path(_seeded_db, tmp_path) -> Path:
    path = tmp_path / "gepeto.db"
    shutil.copy(_seeded_db, path)
    return path


@pytest.fixture
def conn(db_path):
    connection = connect(db_path)
    yield connection
    connection.close()


@pytest.fixture(scope="session")
def golden_profiles() -> dict:
    return {p["id"]: p for p in json.loads((EVALUATION_DIR / "profiles.json").read_text(encoding="utf-8"))}


@pytest.fixture
def make_profile(conn, golden_profiles):
    def make(key: str = "p_ana", **changes) -> int:
        data = {k: v for k, v in golden_profiles[key].items() if k != "id"}
        return save_profile(conn, {**data, **changes})["id"]
    return make


@pytest.fixture
def confirmations() -> Confirmations:
    return Confirmations(SECRET)


@pytest.fixture
def ctx(db_path, make_profile, confirmations, index_dir) -> ToolContext:
    return ToolContext(db_path, make_profile("p_ana"), confirmations, index_dir=index_dir)


@pytest.fixture
def food(conn):
    """fdc_id -> internal food id."""
    return lambda fdc_id: food_id_for(conn, fdc_id)
