"""Pick the 300 foods we use, reproducibly, from the SR Legacy CSV.

Rule: first the foods our 30 recipes need (REQUIRED), then for every category we
take the foods with the shortest descriptions (usually the plain, common items)
until the category quota is full. Ties are broken by fdc_id, so the output never
changes between runs. Foods without kcal/protein/carbs/fat are skipped.

Writes:
    data/source_manifest.csv   fdc_id, description, category, reason
    data/foods_300.jsonl       frozen per-100-g snapshot in our own food format
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gepeto.config import DATA_DIR, USDA_RELEASE  # noqa: E402
from gepeto.importer import NUTRIENT_FIELDS, read_usda_csv  # noqa: E402

# category name -> how many foods (sums to 300)
QUOTAS = {
    "Dairy and Egg Products": 35,
    "Fats and Oils": 15,
    "Poultry Products": 25,
    "Pork Products": 10,
    "Beef Products": 15,
    "Finfish and Shellfish Products": 20,
    "Fruits and Fruit Juices": 40,
    "Vegetables and Vegetable Products": 50,
    "Legumes and Legume Products": 25,
    "Cereal Grains and Pasta": 20,
    "Breakfast Cereals": 5,
    "Baked Products": 10,
    "Nut and Seed Products": 12,
    "Snacks": 8,
    "Spices and Herbs": 5,
    "Sweets": 5,
}

# ingredients used in data/recipes.json (always selected)
REQUIRED = [
    "171077",  # Chicken breast, skinless, boneless, raw
    "171287",  # Egg, whole, raw, fresh
    "171413",  # Oil, olive
    "168877",  # Rice, white, long-grain, raw, enriched
    "169703",  # Rice, brown, long-grain, raw
    "173904",  # Oats, dry
    "172420",  # Lentils, raw
    "173757",  # Chickpeas, cooked, boiled
    "173735",  # Black beans, cooked, boiled
    "172475",  # Tofu, raw, firm
    "175167",  # Salmon, Atlantic, farmed, raw
    "171986",  # Tuna, light, canned in water
    "170026",  # Potatoes, raw
    "170457",  # Tomatoes, red, raw
    "170000",  # Onions, raw
    "169230",  # Garlic, raw
    "170379",  # Broccoli, raw
    "168462",  # Spinach, raw
    "170393",  # Carrots, raw
    "170108",  # Red sweet pepper, raw
    "169251",  # Mushrooms, white, raw
    "173944",  # Bananas, raw
    "171688",  # Apples, raw, with skin
    "171265",  # Milk, whole
    "170903",  # Greek yogurt, plain, lowfat
    "173420",  # Cheese, feta
    "171247",  # Cheese, parmesan, grated
    "170845",  # Cheese, mozzarella
    "169736",  # Pasta, dry
    "172688",  # Bread, whole-wheat
    "172470",  # Peanut butter, smooth
    "169640",  # Honey
    "171796",  # Ground beef 85/15, raw
    "168409",  # Cucumber, raw
    "169247",  # Romaine lettuce, raw
    "171705",  # Avocado, raw
    "168482",  # Sweet potato, raw
    "167762",  # Strawberries, raw
    "171711",  # Blueberries, raw
    "167747",  # Lemon juice, raw
    "173430",  # Butter, without salt
    "168874",  # Quinoa, uncooked
    "168894",  # Wheat flour, white
    "170567",  # Almonds
    "170187",  # Walnuts
    "169291",  # Zucchini, raw
    "169975",  # Cabbage, raw
    "170419",  # Peas, green, raw
    "169998",  # Sweet corn, raw
    "172850",  # Turkey, ground, 93% lean, raw
    "168249",  # Pork tenderloin, raw
    "175179",  # Shrimp, raw
    "172454",  # Hummus, home prepared
    "174081",  # Tortillas, whole wheat
    "169228",  # Eggplant, raw
    "174285",  # Kidney beans, canned, drained
    "170054",  # Tomato sauce, canned
    "170554",  # Chia seeds
    "169097",  # Oranges, raw
    "168421",  # Kale, raw
    "171955",  # Cod, Atlantic, raw
    "169699",  # Couscous, dry
    "170688",  # Bulgur, dry
    "174277",  # Soy sauce
    "172182",  # Cottage cheese, lowfat 2%
    "172446",  # Soymilk, unfortified
    "170173",  # Coconut milk, canned
    "171248",  # Ricotta, part skim
]

# skip odd / processed items when filling quotas
EXCLUDE = re.compile(
    r"babyfood|infant|imitation|variety meats|by-products|dehydrated|"
    r"\b[A-Z]{2,}\b(?<!USDA)",  # upper-case words are usually brand names
)


def main() -> None:
    raw_dir = DATA_DIR / "raw" / USDA_RELEASE
    if not raw_dir.exists():
        raise SystemExit("run scripts/download_usda.py first")

    foods = {food["source_id"]: food for food in read_usda_csv(raw_dir)}
    complete = {
        fdc_id: food for fdc_id, food in foods.items()
        if all(food[name] is not None for name in NUTRIENT_FIELDS)
    }

    chosen: dict[str, str] = {}  # fdc_id -> reason
    for fdc_id in REQUIRED:
        if fdc_id not in complete:
            raise SystemExit(f"required food {fdc_id} missing or incomplete")
        chosen[fdc_id] = "recipe ingredient"

    for category, quota in QUOTAS.items():
        already = sum(1 for fdc_id in chosen if foods[fdc_id]["category"] == category)
        candidates = sorted(
            (
                food for food in complete.values()
                if food["category"] == category
                and food["source_id"] not in chosen
                and not EXCLUDE.search(food["description"])
            ),
            key=lambda food: (len(food["description"]), int(food["source_id"])),
        )
        for food in candidates[: max(0, quota - already)]:
            chosen[food["source_id"]] = "category quota"

    selected = sorted(chosen, key=int)
    assert len(selected) == 300, len(selected)

    with open(DATA_DIR / "source_manifest.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["fdc_id", "description", "category", "reason"])
        for fdc_id in selected:
            writer.writerow([fdc_id, foods[fdc_id]["description"], foods[fdc_id]["category"], chosen[fdc_id]])

    with open(DATA_DIR / "foods_300.jsonl", "w", encoding="utf-8") as handle:
        for fdc_id in selected:
            handle.write(json.dumps(foods[fdc_id], ensure_ascii=False) + "\n")

    print(f"selected {len(selected)} foods -> data/source_manifest.csv, data/foods_300.jsonl")


if __name__ == "__main__":
    main()
