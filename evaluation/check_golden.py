"""Validate the frozen Golden Dataset (evaluation/golden.jsonl).

Structure checks: 60 cases, 10 per category, 6 dev / 4 test per category,
scenario variants never split, every source id exists.

Numeric checks: every expected number is recalculated from the SQLite database
(foods_300.jsonl + recipes.json imported into memory) with gepeto.nutrition and
must match within 1 kcal / 0.1 g. Acceptable recipe ids for suggestions are
recomputed from the stated constraints.

    python evaluation/check_golden.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gepeto.config import DATA_DIR, EVALUATION_DIR  # noqa: E402
from gepeto.db import connect  # noqa: E402
from gepeto.importer import food_id_for, read_food_json, upsert_foods, upsert_recipes  # noqa: E402
from gepeto.nutrition import NUTRIENT_FIELDS, add_up, meal_totals, recipe_per_serving  # noqa: E402

CATEGORIES = [
    "food_lookup", "portion_calculation", "daily_summary",
    "constrained_suggestion", "preference_change", "ambiguous_unsupported",
]
BEHAVIOURS = {
    "answer", "clarify", "report_missing", "report_not_found", "refuse_unsupported",
    "no_match", "confirm_then_write", "no_write_without_confirmation", "reject_invalid",
}
ACTIONS = {
    "get_profile", "get_daily_summary", "search_foods", "search_recipes",
    "calculate_meal", "log_meal", "update_preferences",
}
REQUIRED_KEYS = {
    "id", "category", "scenario", "split", "profile_id", "diary", "turns",
    "expected_behaviour", "expected_facts", "acceptable_recipe_ids", "relevant_source_ids",
    "required_actions", "forbidden_actions", "expected_db_changes",
}
KCAL_TOLERANCE, GRAM_TOLERANCE = 1.0, 0.1


def load_cases(path: Path = EVALUATION_DIR / "golden.jsonl") -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_database():
    conn = connect(":memory:")
    upsert_foods(conn, read_food_json(DATA_DIR / "foods_300.jsonl"))
    upsert_recipes(conn, json.loads((DATA_DIR / "recipes.json").read_text(encoding="utf-8")))
    return conn


def recipe_scaled(conn, recipe_id: str, servings: float) -> dict:
    per = recipe_per_serving(conn, recipe_id)
    return {name: per[name] * servings for name in NUTRIENT_FIELDS}


def diary_totals(conn, diary: list[dict]) -> dict:
    parts = []
    for entry in diary:
        if "fdc_id" in entry:
            parts.append(meal_totals(conn, [(food_id_for(conn, entry["fdc_id"]), entry["grams"])]))
        else:
            parts.append(recipe_scaled(conn, entry["recipe_id"], entry["servings"]))
    return add_up(parts) if parts else {name: 0.0 for name in NUTRIENT_FIELDS}


def calculate(conn, case: dict, profile: dict) -> dict:
    calc = case["calc"]
    if calc["type"] == "foods":
        items = [(food_id_for(conn, item["fdc_id"]), item["grams"]) for item in calc["items"]]
        return meal_totals(conn, items)
    if calc["type"] == "recipe":
        return recipe_scaled(conn, calc["recipe_id"], calc["servings"])
    if calc["type"] == "recipe_cooked_grams":
        recipe = conn.execute("SELECT servings, cooked_weight_g FROM recipes WHERE id = ?", (calc["recipe_id"],)).fetchone()
        total = recipe_scaled(conn, calc["recipe_id"], recipe["servings"])
        return {name: value * calc["grams"] / recipe["cooked_weight_g"] for name, value in total.items()}
    if calc["type"] == "day":
        logged = diary_totals(conn, case["diary"])
        targets = {"kcal": "kcal_target", "protein_g": "protein_target_g", "carbs_g": "carbs_target_g", "fat_g": "fat_target_g"}
        facts = {}
        for name, target in targets.items():
            unit = "" if name == "kcal" else "_g"
            base = name.replace("_g", "")
            facts[f"{base}_logged{unit}"] = logged[name]
            facts[f"{base}_remaining{unit}"] = profile[target] - logged[name]
        return facts
    raise ValueError(f"unknown calc type {calc['type']}")


def matching_recipes(conn, case: dict, profile: dict) -> list[str]:
    rules = case["constraints"]
    max_kcal = rules.get("max_kcal")
    if rules.get("max_kcal_from_remaining"):
        max_kcal = profile["kcal_target"] - diary_totals(conn, case["diary"])["kcal"]
    recipes = {r["id"]: r for r in json.loads((DATA_DIR / "recipes.json").read_text(encoding="utf-8"))}
    found = []
    for recipe_id, recipe in sorted(recipes.items()):
        values = recipe_per_serving(conn, recipe_id)
        tags = set(recipe["tags"])
        names = " ".join(item["name"] for item in recipe["ingredients"])
        diet = rules.get("diet")
        if rules.get("meal_type") and recipe["meal_type"] != rules["meal_type"]:
            continue
        if diet == "pescatarian" and not tags & {"vegetarian", "pescatarian"}:
            continue
        if diet in ("vegetarian", "vegan") and diet not in tags:
            continue
        if rules.get("require_tag") and rules["require_tag"] not in tags:
            continue
        if any(word in names for word in rules.get("exclude_ingredients", [])):
            continue
        if max_kcal is not None and values["kcal"] > max_kcal:
            continue
        if rules.get("min_protein_g") is not None and values["protein_g"] < rules["min_protein_g"]:
            continue
        found.append(recipe_id)
    return found


def check(cases: list[dict] | None = None) -> list[str]:
    cases = load_cases() if cases is None else cases
    profiles = {p["id"]: p for p in json.loads((EVALUATION_DIR / "profiles.json").read_text(encoding="utf-8"))}
    conn = build_database()
    known_foods = {row["source_id"] for row in conn.execute("SELECT source_id FROM foods")}
    known_recipes = {row["id"] for row in conn.execute("SELECT id FROM recipes")}
    errors: list[str] = []

    if len(cases) != 60:
        errors.append(f"expected 60 cases, found {len(cases)}")
    if len({case.get("id") for case in cases}) != len(cases):
        errors.append("case ids are not unique")
    per_category = Counter(case.get("category") for case in cases)
    per_split = Counter((case.get("category"), case.get("split")) for case in cases)
    for category in CATEGORIES:
        if per_category[category] != 10:
            errors.append(f"{category}: {per_category[category]} cases, expected 10")
        if per_split[(category, "dev")] != 6 or per_split[(category, "test")] != 4:
            errors.append(f"{category}: split is not 6 dev / 4 test")
    scenario_splits = defaultdict(set)
    for case in cases:
        scenario_splits[case.get("scenario")].add(case.get("split"))
    for scenario, splits in scenario_splits.items():
        if len(splits) > 1:
            errors.append(f"scenario {scenario} appears in both splits")

    for case in cases:
        cid = case.get("id")
        missing = REQUIRED_KEYS - case.keys()
        if missing:
            errors.append(f"{cid}: missing keys {sorted(missing)}")
            continue
        if case["category"] not in CATEGORIES:
            errors.append(f"{cid}: unknown category")
        if case["expected_behaviour"] not in BEHAVIOURS:
            errors.append(f"{cid}: unknown behaviour {case['expected_behaviour']}")
        if case["profile_id"] not in profiles:
            errors.append(f"{cid}: unknown profile {case['profile_id']}")
            continue
        if not case["turns"] or not all(isinstance(turn, str) and turn.strip() for turn in case["turns"]):
            errors.append(f"{cid}: needs at least one non-empty turn")
        for action in case["required_actions"] + case["forbidden_actions"]:
            if action not in ACTIONS:
                errors.append(f"{cid}: unknown action {action}")
        if set(case["required_actions"]) & set(case["forbidden_actions"]):
            errors.append(f"{cid}: action both required and forbidden")
        if case["expected_db_changes"] and case["expected_behaviour"] != "confirm_then_write":
            errors.append(f"{cid}: only confirmed writes may change the database")
        for source in case["relevant_source_ids"]:
            kind, _, ident = source.partition(":")
            if (kind == "usda" and ident not in known_foods) or (kind == "recipe" and ident not in known_recipes) or kind not in ("usda", "recipe"):
                errors.append(f"{cid}: unknown source {source}")
        for entry in case["diary"]:
            if entry.get("fdc_id", "") not in known_foods and entry.get("recipe_id", "") not in known_recipes:
                errors.append(f"{cid}: diary entry {entry} not in data")

        profile = profiles[case["profile_id"]]
        if case["expected_facts"]:
            if "calc" not in case:
                errors.append(f"{cid}: numeric case needs a calc spec")
                continue
            actual = calculate(conn, case, profile)
            for name, expected in case["expected_facts"].items():
                value = actual.get(name)
                tolerance = KCAL_TOLERANCE if name.startswith("kcal") else GRAM_TOLERANCE
                if value is None or abs(value - expected) > tolerance:
                    errors.append(f"{cid}: {name} expected {expected}, recalculated {value}")
        if "constraints" in case:
            recomputed = matching_recipes(conn, case, profile)
            if sorted(case["acceptable_recipe_ids"]) != recomputed:
                errors.append(f"{cid}: acceptable recipes {case['acceptable_recipe_ids']} != recomputed {recomputed}")
            if case["expected_behaviour"] == "no_match" and recomputed:
                errors.append(f"{cid}: marked no_match but recipes fit")
    return errors


def main() -> None:
    errors = check()
    for error in errors:
        print("FAIL", error)
    cases = load_cases()
    print(f"{len(cases)} cases, {Counter(c['split'] for c in cases)}, {len(errors)} problems")
    raise SystemExit(1 if errors else 0)


if __name__ == "__main__":
    main()
