"""Deterministic numeric tests for the diary and nutrient calculations.

Expected values are worked out by hand (or with independent arithmetic straight
from the JSON data files), not with gepeto.nutrition.
"""

import json

import pytest

from gepeto import tools
from gepeto.config import DATA_DIR
from gepeto.errors import GepetoError
from gepeto.importer import upsert_foods
from gepeto.tools import ToolContext

from conftest import DAY

FOODS = {f["source_id"]: f for f in map(json.loads, (DATA_DIR / "foods_300.jsonl").read_text().splitlines())}
RECIPES = {r["id"]: r for r in json.loads((DATA_DIR / "recipes.json").read_text())}
FIELDS = ("kcal", "protein_g", "carbs_g", "fat_g")


def by_hand_recipe_total(recipe_id: str) -> dict:
    recipe = RECIPES[recipe_id]
    return {n: sum(FOODS[i["fdc_id"]][n] * i["grams"] / 100 for i in recipe["ingredients"]) for n in FIELDS}


def log(ctx, items, request_id, day=DAY):
    preview = tools.calculate_meal(ctx, items, day)
    token = ctx.confirmations.meal_token(ctx.profile_id, preview["preview_id"])
    return tools.log_meal(ctx, preview["preview_id"], token, request_id)


def test_food_portion_is_scaled_from_100_g(ctx, food) -> None:
    # USDA 171077 raw chicken breast: 120 kcal, 22.5 g protein, 0 g carbs, 2.62 g fat per 100 g
    preview = tools.calculate_meal(ctx, [{"food_id": food("171077"), "grams": 200}], DAY)
    assert preview["totals"] == {"kcal": 240.0, "protein_g": 45.0, "carbs_g": 0.0, "fat_g": 5.2}
    assert preview["items"][0]["source_id"] == "usda:171077" and preview["estimate"] is False


def test_multi_item_meal_adds_up(ctx, food) -> None:
    items = [{"food_id": food("171287"), "grams": 100}, {"food_id": food("172688"), "grams": 60}]
    expected = sum(FOODS[fdc]["kcal"] * g / 100 for fdc, g in (("171287", 100), ("172688", 60)))
    preview = tools.calculate_meal(ctx, items, DAY)
    assert preview["totals"]["kcal"] == round(expected)
    assert sum(item["nutrients"]["kcal"] for item in preview["items"]) == pytest.approx(expected, abs=1)


@pytest.mark.parametrize("recipe_id, servings", [("r08", 1), ("r07", 2), ("r14", 0.5)])
def test_recipe_by_servings(ctx, recipe_id, servings) -> None:
    total = by_hand_recipe_total(recipe_id)
    per = {n: total[n] / RECIPES[recipe_id]["servings"] * servings for n in FIELDS}
    preview = tools.calculate_meal(ctx, [{"recipe_id": recipe_id, "servings": servings}], DAY)
    assert preview["totals"]["kcal"] == pytest.approx(per["kcal"], abs=0.5)
    for name in ("protein_g", "carbs_g", "fat_g"):
        assert preview["totals"][name] == pytest.approx(per[name], abs=0.05)
    assert preview["estimate"] is True


def test_recipe_by_cooked_grams(ctx) -> None:
    # r08 lentil soup: whole pot weighs 1600 g cooked; 300 g is 300/1600 of the pot.
    expected = by_hand_recipe_total("r08")["kcal"] * 300 / 1600
    preview = tools.calculate_meal(ctx, [{"recipe_id": "r08", "grams": 300}], DAY)
    assert preview["totals"]["kcal"] == pytest.approx(expected, abs=0.5)
    assert preview["items"][0]["servings"] == pytest.approx(300 / 1600 * RECIPES["r08"]["servings"], abs=1e-3)


def test_recipe_without_cooked_weight_cannot_be_logged_by_grams(ctx) -> None:
    with pytest.raises(GepetoError) as error:
        tools.calculate_meal(ctx, [{"recipe_id": "r01", "grams": 250}], DAY)
    assert error.value.code == "missing_data"


@pytest.mark.parametrize("item, code", [
    ({"food_id": 1, "grams": 0}, "invalid_input"),
    ({"food_id": 1, "grams": -5}, "invalid_input"),
    ({"food_id": 1, "grams": 6000}, "invalid_input"),
    ({"food_id": 1, "grams": "200"}, "invalid_input"),
    ({"food_id": 1, "servings": 1}, "invalid_input"),
    ({"recipe_id": "r08", "servings": 0}, "invalid_input"),
    ({"recipe_id": "r08", "servings": 1, "grams": 300}, "invalid_input"),
    ({"food_id": 1, "recipe_id": "r08", "grams": 1}, "invalid_input"),
    ({"food_id": 999999, "grams": 100}, "not_found"),
    ({"recipe_id": "r99", "servings": 1}, "not_found"),
])
def test_invalid_quantities_and_unknown_records(ctx, item, code) -> None:
    with pytest.raises(GepetoError) as error:
        tools.calculate_meal(ctx, [item], DAY)
    assert error.value.code == code


@pytest.mark.parametrize("day", ["2026-02-30", "20-11-2026", "today"])
def test_bad_dates_are_rejected(ctx, day) -> None:
    with pytest.raises(GepetoError) as error:
        tools.get_daily_summary(ctx, day)
    assert error.value.code == "invalid_input"


def test_empty_and_oversized_meals_are_rejected(ctx, food) -> None:
    for items in ([], [{"food_id": food("171077"), "grams": 10}] * 21):
        with pytest.raises(GepetoError):
            tools.calculate_meal(ctx, items, DAY)


def test_preview_does_not_write_the_diary(ctx, conn, food) -> None:
    tools.calculate_meal(ctx, [{"food_id": food("171077"), "grams": 200}], DAY)
    assert conn.execute("SELECT COUNT(*) FROM meal_logs").fetchone()[0] == 0
    assert tools.get_daily_summary(ctx, DAY)["entry_count"] == 0


def test_empty_day(ctx) -> None:
    summary = tools.get_daily_summary(ctx, DAY)
    assert summary["totals"] == {"kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
    assert summary["remaining"] == {"kcal": 2000.0, "protein_g": 100.0, "carbs_g": 250.0, "fat_g": 67.0}


def test_day_totals_and_remaining(ctx, food) -> None:
    log(ctx, [{"food_id": food("171077"), "grams": 200}], "req-day-0001")          # 240 kcal, 45 g protein
    log(ctx, [{"recipe_id": "r07", "servings": 1}], "req-day-0002")
    log(ctx, [{"food_id": food("171077"), "grams": 100}], "req-day-0003", "2026-11-19")  # other day
    r07 = by_hand_recipe_total("r07")["kcal"] / RECIPES["r07"]["servings"]
    summary = tools.get_daily_summary(ctx, DAY)
    assert summary["entry_count"] == 2 and len(summary["meals"]) == 2
    assert summary["totals"]["kcal"] == pytest.approx(240 + r07, abs=0.5)
    assert summary["remaining"]["kcal"] == pytest.approx(2000 - 240 - r07, abs=0.5)
    assert {item["source_id"] for meal in summary["meals"] for item in meal["items"]} == {"usda:171077", "recipe:r07"}


def test_going_over_target_gives_negative_remaining_and_a_failed_check(ctx) -> None:
    log(ctx, [{"recipe_id": "r14", "servings": 2}, {"recipe_id": "r18", "servings": 2}], "req-over-001")
    summary = tools.get_daily_summary(ctx, DAY)
    assert summary["remaining"]["kcal"] < 0
    preview = tools.calculate_meal(ctx, [{"recipe_id": "r01", "servings": 1}], DAY)
    assert {"rule": "daily_kcal_target", "status": "fail"}.items() <= preview["checks"][-1].items()


def test_unknown_nutrient_stays_unknown(db_path, conn, make_profile, confirmations) -> None:
    upsert_foods(conn, [{"description": "Mystery bar", "kcal": 400, "protein_g": 10, "carbs_g": 60,
                         "source_note": "label photo, fat not printed"}])
    bar = conn.execute("SELECT id FROM foods WHERE description = 'Mystery bar'").fetchone()["id"]
    ctx = ToolContext(db_path, make_profile("p_ana"), confirmations)
    preview = tools.calculate_meal(ctx, [{"food_id": bar, "grams": 50}], DAY)
    assert preview["totals"] == {"kcal": 200.0, "protein_g": 5.0, "carbs_g": 30.0, "fat_g": None}
    assert preview["unknown_fields"] == ["fat_g"]
    log(ctx, [{"food_id": bar, "grams": 50}], "req-unknown-1")
    summary = tools.get_daily_summary(ctx, DAY)
    assert summary["totals"]["fat_g"] is None and summary["remaining"]["fat_g"] is None
    assert summary["totals"]["kcal"] == 200.0 and summary["unknown_fields"] == ["fat_g"]


def test_rule_checks_use_profile_restrictions(db_path, make_profile, confirmations, food) -> None:
    ioana = ToolContext(db_path, make_profile("p_ioana"), confirmations)  # vegetarian
    checks = lambda items: {c["rule"]: c["status"] for c in tools.calculate_meal(ioana, items, DAY)["checks"]}
    assert checks([{"food_id": food("171077"), "grams": 100}])["diet:vegetarian"] == "fail"   # chicken
    assert checks([{"food_id": food("171413"), "grams": 10}])["diet:vegetarian"] == "unknown"  # oil category
    assert checks([{"recipe_id": "r24", "servings": 1}])["diet:vegetarian"] == "pass"
    dan = ToolContext(db_path, make_profile("p_dan"), confirmations)  # pescatarian, dislikes mushrooms
    result = {c["rule"]: c["status"] for c in tools.calculate_meal(dan, [{"recipe_id": "r15", "servings": 1}], DAY)["checks"]}
    assert result == {"diet:pescatarian": "pass", "exclude:mushrooms": "fail", "daily_kcal_target": "pass"}


def test_logged_totals_do_not_change_when_food_data_is_reimported(ctx, conn, food) -> None:
    log(ctx, [{"food_id": food("171077"), "grams": 200}], "req-snap-0001")
    changed = {**FOODS["171077"], "kcal": 150.0}
    assert upsert_foods(conn, [changed]).updated == 1
    assert tools.get_daily_summary(ctx, DAY)["totals"]["kcal"] == 240.0       # old snapshot
    preview = tools.calculate_meal(ctx, [{"food_id": food("171077"), "grams": 200}], DAY)
    assert preview["totals"]["kcal"] == 300.0                                 # new data for new meals
    row = conn.execute("SELECT food_version FROM meal_logs").fetchone()
    assert row["food_version"] == 1
