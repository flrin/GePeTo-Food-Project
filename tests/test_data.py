import csv
import json

from gepeto.config import DATA_DIR
from gepeto.db import connect
from gepeto.importer import read_food_json, upsert_foods, upsert_recipes
from gepeto.nutrition import recipe_per_serving


def manifest() -> list[dict]:
    with open(DATA_DIR / "source_manifest.csv", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_manifest_has_300_unique_foods() -> None:
    ids = [row["fdc_id"] for row in manifest()]
    assert len(ids) == 300 == len(set(ids))


def test_snapshot_matches_manifest_and_is_complete() -> None:
    foods = read_food_json(DATA_DIR / "foods_300.jsonl")
    assert sorted(f["source_id"] for f in foods) == sorted(r["fdc_id"] for r in manifest())
    assert all(f["source_version"] == "SR Legacy 2018-04" for f in foods)
    assert all(f[n] is not None for f in foods for n in ("kcal", "protein_g", "carbs_g", "fat_g"))


def test_30_recipes_import_and_have_estimates() -> None:
    recipes = json.loads((DATA_DIR / "recipes.json").read_text(encoding="utf-8"))
    assert len(recipes) == 30
    conn = connect()
    upsert_foods(conn, read_food_json(DATA_DIR / "foods_300.jsonl"))
    report = upsert_recipes(conn, recipes)
    assert report.rejected == [] and report.inserted == 30
    for recipe in recipes:
        values = recipe_per_serving(conn, recipe["id"])
        assert values["estimate"] is True and values["kcal"] > 0


def test_recipe_with_unknown_ingredient_is_rejected() -> None:
    conn = connect()
    bad = {"id": "rx", "title": "x", "meal_type": "lunch", "servings": 1, "weight_state": "raw",
           "ingredients": [{"fdc_id": "999999", "grams": 10}], "steps": ["eat"]}
    report = upsert_recipes(conn, [bad])
    assert report.rejected and "unknown ingredient" in report.rejected[0][1][0]
