"""Import foods and recipes into SQLite.

Usage:
    python -m gepeto.importer usda data/raw/<release> --manifest data/source_manifest.csv
    python -m gepeto.importer foods data/foods_300.jsonl
    python -m gepeto.importer foods my_packaged_foods.json      (user-entered foods)
    python -m gepeto.importer recipes data/recipes.json

Steps (Milestone 2, section 2):
 1. validate required fields, units and non-negative values, report rejected rows
 2. normalize nutrients to 100 g (servings only when the gram weight is known)
 3. insert or update by (source, source_id), skipping unchanged rows by content hash
 (4. search indexes are rebuilt in week 9)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import USDA_SOURCE, USDA_SOURCE_VERSION, Settings
from .db import connect


# FoodData Central nutrient ids -> our column names
USDA_NUTRIENTS = {
    "1008": "kcal",       # Energy (KCAL)
    "1003": "protein_g",  # Protein
    "1005": "carbs_g",    # Carbohydrate, by difference
    "1004": "fat_g",      # Total lipid (fat)
}
NUTRIENT_FIELDS = ["kcal", "protein_g", "carbs_g", "fat_g"]
HASHED_FIELDS = ["description", "category", "prep_state", *NUTRIENT_FIELDS, "source_version"]

COOKED_WORDS = (
    "cooked", "boiled", "roasted", "baked", "fried", "grilled", "broiled",
    "braised", "steamed", "stewed", "microwaved", "toasted", "canned", "pan-browned",
)
RAW_WORDS = ("raw", "uncooked", "dry")
KJ_PER_KCAL = 4.184


@dataclass
class ImportReport:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    rejected: list[tuple[str, list[str]]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"inserted={self.inserted} updated={self.updated} "
            f"unchanged={self.unchanged} rejected={len(self.rejected)}"
        )


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def guess_prep_state(description: str) -> str:
    words = re.findall(r"[a-z-]+", description.lower())
    if any(word in COOKED_WORDS for word in words):
        return "cooked"
    if any(word in RAW_WORDS for word in words):
        return "raw"
    return "unspecified"


def content_hash(record: dict) -> str:
    payload = json.dumps({key: record.get(key) for key in HASHED_FIELDS}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- readers


def read_usda_csv(directory: Path, fdc_ids: set[str] | None = None) -> list[dict]:
    """Read SR Legacy CSV files and return records in our food format (per 100 g)."""
    directory = Path(directory)
    categories = {
        row["id"]: row["description"]
        for row in csv.DictReader(open(directory / "food_category.csv", encoding="utf-8"))
    }
    records: dict[str, dict] = {}
    with open(directory / "food.csv", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if fdc_ids is not None and row["fdc_id"] not in fdc_ids:
                continue
            records[row["fdc_id"]] = {
                "source": USDA_SOURCE,
                "source_id": row["fdc_id"],
                "description": row["description"],
                "category": categories.get(row["food_category_id"]),
                "prep_state": guess_prep_state(row["description"]),
                # missing nutrients stay None (= NULL), never 0
                **{name: None for name in NUTRIENT_FIELDS},
                "source_version": USDA_SOURCE_VERSION,
                "user_entered": False,
                "source_note": None,
            }
    with open(directory / "food_nutrient.csv", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            record = records.get(row["fdc_id"])
            column = USDA_NUTRIENTS.get(row["nutrient_id"])
            if record is not None and column and row["amount"] != "":
                record[column] = float(row["amount"])  # SR Legacy is already per 100 g
    return list(records.values())


def read_food_json(path: Path) -> list[dict]:
    """Read our own food format: a JSON list or JSON lines."""
    text = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return json.loads(text)


# ---------------------------------------------------------------- validation


def normalize_food(raw: dict) -> tuple[dict | None, list[str]]:
    """Validate one food row and convert it to per-100-g kcal/grams.

    Our JSON format (user-entered packaged foods) may give label values per serving:
      {"description": "Granola bar", "basis": "serving", "serving_g": 40,
       "energy_unit": "kJ", "kcal": 800, "protein_g": 4, ..., "source_note": "label photo"}
    """
    errors: list[str] = []
    record = dict(raw)
    record.setdefault("source", "user")
    record.setdefault("source_version", "user-entered")
    record.setdefault("category", None)
    record["user_entered"] = bool(record.get("user_entered", record["source"] == "user"))

    if not str(record.get("description") or "").strip():
        errors.append("missing description")
    if record["user_entered"]:
        if not str(record.get("source_note") or "").strip():
            errors.append("user-entered food needs a source_note")
        if not str(record.get("source_id") or "").strip():
            # stable id for user foods: derived from the name
            record["source_id"] = re.sub(r"[^a-z0-9]+", "-", str(record.get("description", "")).lower()).strip("-")
    if not str(record.get("source_id") or "").strip():
        errors.append("missing source_id")

    for name in NUTRIENT_FIELDS:
        value = record.get(name)
        if value is None:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            errors.append(f"{name} is not a number")
        elif value < 0:
            errors.append(f"{name} is negative")
    if record.get("kcal") is None and record.get("protein_g") is None:
        errors.append("needs at least kcal or protein_g")

    unit = record.pop("energy_unit", "kcal")
    if unit not in ("kcal", "kJ"):
        errors.append(f"unknown energy_unit {unit!r}")

    basis = record.pop("basis", "100g")
    serving_g = record.pop("serving_g", None)
    if basis not in ("100g", "serving"):
        errors.append(f"unknown basis {basis!r}")
    if basis == "serving" and not (isinstance(serving_g, (int, float)) and serving_g > 0):
        errors.append("basis 'serving' needs a positive serving_g")

    if errors:
        return None, errors

    if unit == "kJ" and record.get("kcal") is not None:
        record["kcal"] = record["kcal"] / KJ_PER_KCAL
    if basis == "serving":
        factor = 100 / serving_g
        for name in NUTRIENT_FIELDS:
            if record.get(name) is not None:
                record[name] = record[name] * factor
    for name in NUTRIENT_FIELDS:
        record[name] = None if record.get(name) is None else round(float(record[name]), 2)
    record.setdefault("prep_state", guess_prep_state(record["description"]))
    record["source_id"] = str(record["source_id"])
    return record, []


# ---------------------------------------------------------------- writers


def upsert_foods(conn: sqlite3.Connection, rows: list[dict]) -> ImportReport:
    report = ImportReport()
    for raw in rows:
        record, errors = normalize_food(raw)
        if record is None:
            report.rejected.append((str(raw.get("source_id") or raw.get("description")), errors))
            continue
        digest = content_hash(record)
        existing = conn.execute(
            "SELECT id, content_hash FROM foods WHERE source = ? AND source_id = ?",
            (record["source"], record["source_id"]),
        ).fetchone()
        values = (
            record["description"], record["category"], record["prep_state"],
            record["kcal"], record["protein_g"], record["carbs_g"], record["fat_g"],
            record["source_version"], now(), digest,
            int(record["user_entered"]), record.get("source_note"),
        )
        if existing is None:
            conn.execute(
                """INSERT INTO foods (description, category, prep_state, kcal, protein_g,
                       carbs_g, fat_g, source_version, import_date, content_hash,
                       user_entered, source_note, source, source_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*values, record["source"], record["source_id"]),
            )
            report.inserted += 1
        elif existing["content_hash"] == digest:
            report.unchanged += 1
        else:
            conn.execute(
                """UPDATE foods SET description = ?, category = ?, prep_state = ?, kcal = ?,
                       protein_g = ?, carbs_g = ?, fat_g = ?, source_version = ?,
                       import_date = ?, content_hash = ?, user_entered = ?, source_note = ?,
                       version = version + 1
                   WHERE id = ?""",
                (*values, existing["id"]),
            )
            report.updated += 1
    _log_run(conn, "foods", report)
    return report


def food_id_for(conn: sqlite3.Connection, fdc_id: str, source: str = USDA_SOURCE) -> int | None:
    row = conn.execute(
        "SELECT id FROM foods WHERE source = ? AND source_id = ?", (source, str(fdc_id))
    ).fetchone()
    return None if row is None else row["id"]


def validate_recipe(conn: sqlite3.Connection, recipe: dict) -> list[str]:
    errors = []
    for key in ("id", "title", "meal_type", "servings", "weight_state", "ingredients", "steps"):
        if not recipe.get(key):
            errors.append(f"missing {key}")
    if errors:
        return errors
    if not isinstance(recipe["servings"], int) or recipe["servings"] <= 0:
        errors.append("servings must be a positive integer")
    if recipe["weight_state"] not in ("raw", "cooked"):
        errors.append("weight_state must be 'raw' or 'cooked'")
    seen = set()
    for item in recipe["ingredients"]:
        if food_id_for(conn, item.get("fdc_id", "")) is None:
            errors.append(f"unknown ingredient fdc_id {item.get('fdc_id')}")
        if not (isinstance(item.get("grams"), (int, float)) and item["grams"] > 0):
            errors.append(f"ingredient {item.get('fdc_id')} needs positive grams")
        if item.get("fdc_id") in seen:
            errors.append(f"duplicate ingredient {item.get('fdc_id')}")
        seen.add(item.get("fdc_id"))
    return errors


def upsert_recipes(conn: sqlite3.Connection, recipes: list[dict]) -> ImportReport:
    report = ImportReport()
    for recipe in recipes:
        errors = validate_recipe(conn, recipe)
        if errors:
            report.rejected.append((str(recipe.get("id")), errors))
            continue
        digest = hashlib.sha256(json.dumps(recipe, sort_keys=True).encode("utf-8")).hexdigest()
        existing = conn.execute(
            "SELECT content_hash FROM recipes WHERE id = ?", (recipe["id"],)
        ).fetchone()
        if existing is not None and existing["content_hash"] == digest:
            report.unchanged += 1
            continue
        conn.execute("DELETE FROM recipes WHERE id = ?", (recipe["id"],))  # cascades ingredients
        conn.execute(
            """INSERT INTO recipes (id, title, meal_type, servings, weight_state,
                   cooked_weight_g, tags, steps, content_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                recipe["id"], recipe["title"], recipe["meal_type"], recipe["servings"],
                recipe["weight_state"], recipe.get("cooked_weight_g"),
                json.dumps(recipe.get("tags", [])), json.dumps(recipe["steps"]), digest,
            ),
        )
        conn.executemany(
            "INSERT INTO recipe_ingredients (recipe_id, food_id, grams) VALUES (?, ?, ?)",
            [(recipe["id"], food_id_for(conn, item["fdc_id"]), item["grams"]) for item in recipe["ingredients"]],
        )
        if existing is None:
            report.inserted += 1
        else:
            report.updated += 1
    _log_run(conn, "recipes", report)
    return report


def _log_run(conn: sqlite3.Connection, source: str, report: ImportReport) -> None:
    conn.execute(
        """INSERT INTO import_runs (started_at, source, inserted, updated, unchanged, rejected)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (now(), source, report.inserted, report.updated, report.unchanged, len(report.rejected)),
    )
    conn.commit()


def read_manifest_ids(path: Path) -> set[str]:
    with open(path, encoding="utf-8") as handle:
        return {row["fdc_id"] for row in csv.DictReader(handle)}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("kind", choices=["usda", "foods", "recipes"])
    parser.add_argument("path", type=Path)
    parser.add_argument("--manifest", type=Path, help="only import fdc_ids listed in this CSV")
    parser.add_argument("--db", type=Path, default=Settings.from_environment().db_path)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    if args.kind == "usda":
        ids = read_manifest_ids(args.manifest) if args.manifest else None
        report = upsert_foods(conn, read_usda_csv(args.path, ids))
    elif args.kind == "foods":
        report = upsert_foods(conn, read_food_json(args.path))
    else:
        report = upsert_recipes(conn, json.loads(args.path.read_text(encoding="utf-8")))

    print(f"{args.kind}: {report.summary()}  ->  {args.db}")
    for source_id, errors in report.rejected:
        print(f"  rejected {source_id}: {'; '.join(errors)}")


if __name__ == "__main__":
    main()
