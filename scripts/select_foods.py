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
from gepeto.config import DATA_DIR, USDA_RELEASE
from gepeto.importer import NUTRIENT_FIELDS, read_usda_csv

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

REQUIRED = [
    "171077",
    "171287",
    "171413",
    "168877",
    "169703",
    "173904",
    "172420",
    "173757",
    "173735",
    "172475",
    "175167",
    "171986",
    "170026",
    "170457",
    "170000",
    "169230",
    "170379",
    "168462",
    "170393",
    "170108",
    "169251",
    "173944",
    "171688",
    "171265",
    "170903",
    "173420",
    "171247",
    "170845",
    "169736",
    "172688",
    "172470",
    "169640",
    "171796",
    "168409",
    "169247",
    "171705",
    "168482",
    "167762",
    "171711",
    "167747",
    "173430",
    "168874",
    "168894",
    "170567",
    "170187",
    "169291",
    "169975",
    "170419",
    "169998",
    "172850",
    "168249",
    "175179",
    "172454",
    "174081",
    "169228",
    "174285",
    "170054",
    "170554",
    "169097",
    "168421",
    "171955",
    "169699",
    "170688",
    "174277",
    "172182",
    "172446",
    "170173",
    "171248",
]

EXCLUDE = re.compile(
    r"babyfood|infant|imitation|variety meats|by-products|dehydrated|"
    r"\b[A-Z]{2,}\b(?<!USDA)",
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

    chosen: dict[str, str] = {}
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
