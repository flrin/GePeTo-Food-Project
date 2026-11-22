import json
import shutil
from pathlib import Path

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
def ctx(db_path, make_profile, confirmations) -> ToolContext:
    return ToolContext(db_path, make_profile("p_ana"), confirmations)


@pytest.fixture
def food(conn):
    """fdc_id -> internal food id."""
    return lambda fdc_id: food_id_for(conn, fdc_id)
