"""Run the golden numeric, suggestion and preference cases through the real MCP tools.

check_golden.py (week 7) recomputes the reference answers with gepeto.nutrition.
This script asks the *tools* instead, over an MCP client session, the same way
the assistant will: diaries are seeded with calculate_meal + log_meal, numbers
come from calculate_meal / get_daily_summary, suggestions from search_recipes,
and confirmed preference changes go through update_preferences.

Only the dev split is used by default: the test split stays untouched until week 12.

    python evaluation/check_tools.py                  # dev split -> evaluation/runs/week8_tools.json
    python evaluation/check_tools.py --out -          # print only
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

import anyio
from mcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evaluation.check_golden import GRAM_TOLERANCE, KCAL_TOLERANCE, load_cases
from gepeto.config import DATA_DIR, EVALUATION_DIR
from gepeto.confirm import Confirmations
from gepeto.db import connect
from gepeto.importer import food_id_for, read_food_json, upsert_foods, upsert_recipes
from gepeto.mcp_host import parse_tool_error
from gepeto.mcp_server import build_server
from gepeto.profiles import save_profile
from gepeto.tools import ToolContext

DAY = "2026-11-16"  # any fixed date; nothing depends on today's date
DAY_FACTS = {  # golden fact name -> (summary section, nutrient)
    "kcal_logged": ("totals", "kcal"), "kcal_remaining": ("remaining", "kcal"),
    "protein_logged_g": ("totals", "protein_g"), "protein_remaining_g": ("remaining", "protein_g"),
    "carbs_logged_g": ("totals", "carbs_g"), "carbs_remaining_g": ("remaining", "carbs_g"),
    "fat_logged_g": ("totals", "fat_g"), "fat_remaining_g": ("remaining", "fat_g"),
}


def build_database(path: Path) -> None:
    conn = connect(path)
    upsert_foods(conn, read_food_json(DATA_DIR / "foods_300.jsonl"))
    upsert_recipes(conn, json.loads((DATA_DIR / "recipes.json").read_text(encoding="utf-8")))
    conn.close()


class Session:
    """An MCP client session bound to one fresh profile, like the app will have."""

    def __init__(self, client: Client, profile_id: int, confirmations: Confirmations):
        self.client, self.profile_id, self.confirmations = client, profile_id, confirmations
        self.calls: list[str] = []

    async def call(self, name: str, arguments: dict) -> dict:
        self.calls.append(name)
        result = await self.client.call_tool(name, arguments)
        if result.is_error:
            raise parse_tool_error(" ".join(part.text for part in result.content))
        return result.structured_content

    async def log(self, items: list[dict], request_id: str) -> dict:
        preview = await self.call("calculate_meal", {"items": items, "date": DAY})
        token = self.confirmations.meal_token(self.profile_id, preview["preview_id"])
        return await self.call("log_meal", {
            "preview_id": preview["preview_id"], "confirmation_token": token, "request_id": request_id})

    async def confirm_change(self, change: dict) -> dict:
        from gepeto.profiles import normalize_change
        change = normalize_change(change)
        token = self.confirmations.preference_token(self.profile_id, change)
        return await self.call("update_preferences", {"change": change, "confirmation_token": token})


def item_for(db_path: Path, entry: dict) -> dict:
    if "fdc_id" in entry:
        conn = connect(db_path)
        try:
            return {"food_id": food_id_for(conn, entry["fdc_id"]), "grams": entry["grams"]}
        finally:
            conn.close()
    if "servings" in entry:
        return {"recipe_id": entry["recipe_id"], "servings": entry["servings"]}
    return {"recipe_id": entry["recipe_id"], "grams": entry["grams"]}


def compare(name: str, expected, actual) -> dict:
    tolerance = KCAL_TOLERANCE if name.startswith("kcal") else GRAM_TOLERANCE
    ok = actual is not None and abs(actual - expected) <= tolerance
    return {"check": name, "expected": expected, "actual": actual, "ok": ok}


async def run_case(case: dict, profile: dict, db_path: Path, confirmations: Confirmations) -> dict:
    conn = connect(db_path)
    profile_id = save_profile(conn, {k: v for k, v in profile.items() if k != "id"})["id"]
    conn.close()
    server = build_server(ToolContext(db_path, profile_id, confirmations))
    checks: list[dict] = []
    async with Client(server) as client:
        session = Session(client, profile_id, confirmations)
        for index, entry in enumerate(case["diary"]):
            await session.log([item_for(db_path, entry)], f"seed-{case['id']}-{index}")

        for change in case["expected_db_changes"]:
            op = next(key for key in ("add", "remove", "set") if key in change)
            await session.confirm_change({"field": change["field"], "op": op, "value": change[op]})
            stored = await session.call("get_profile", {})
            if op == "set":
                value = stored["targets"][change["field"]]
                expected_ok = value == change["set"]
            else:
                value = stored[change["field"]]
                expected_ok = (change[op] in value) == (op == "add")
            checks.append({"check": f"db:{change['field']}:{op}", "expected": change[op], "actual": value, "ok": expected_ok})

        calc = case.get("calc")
        if case["expected_facts"] and calc:
            if calc["type"] == "day":
                summary = await session.call("get_daily_summary", {"date": DAY})
                for name, expected in case["expected_facts"].items():
                    section, nutrient = DAY_FACTS[name]
                    checks.append(compare(name, expected, summary[section][nutrient]))
            else:
                if calc["type"] == "foods":
                    items = [item_for(db_path, item) for item in calc["items"]]
                elif calc["type"] == "recipe":
                    items = [{"recipe_id": calc["recipe_id"], "servings": calc["servings"]}]
                else:  # recipe_cooked_grams
                    items = [{"recipe_id": calc["recipe_id"], "grams": calc["grams"]}]
                preview = await session.call("calculate_meal", {"items": items, "date": DAY})
                for name, expected in case["expected_facts"].items():
                    checks.append(compare(name, expected, preview["totals"][name]))

        if "constraints" in case:
            rules = case["constraints"]
            args = {"limit": 10}
            for key in ("meal_type", "diet", "require_tag", "exclude_ingredients", "max_kcal", "min_protein_g"):
                if rules.get(key) is not None:
                    args[key] = rules[key]
            if rules.get("max_kcal_from_remaining"):
                args["fit_remaining_kcal_on"] = DAY
            found = await session.call("search_recipes", args)
            ids = sorted(result["recipe_id"] for result in found["results"])
            checks.append({"check": "acceptable_recipe_ids", "expected": sorted(case["acceptable_recipe_ids"]),
                           "actual": ids, "ok": ids == sorted(case["acceptable_recipe_ids"])})
    return {"id": case["id"], "category": case["category"], "split": case["split"],
            "tool_calls": Counter(session.calls), "checks": checks}


async def run(split: str = "dev") -> dict:
    profiles = {p["id"]: p for p in json.loads((EVALUATION_DIR / "profiles.json").read_text(encoding="utf-8"))}
    cases = [c for c in load_cases() if split == "all" or c["split"] == split]
    cases = [c for c in cases if c["expected_facts"] or "constraints" in c or c["expected_db_changes"]]
    confirmations = Confirmations("evaluation-only-secret")
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "eval.db"
        build_database(db_path)
        results = [await run_case(case, profiles[case["profile_id"]], db_path, confirmations) for case in cases]
    checks = [check for result in results for check in result["checks"]]
    numeric = [check for check in checks if not check["check"].startswith(("db:", "acceptable"))]
    return {
        "split": split,
        "cases": len(results),
        "summary": {
            "numeric_checks": len(numeric), "numeric_ok": sum(c["ok"] for c in numeric),
            "suggestion_checks": sum(c["check"] == "acceptable_recipe_ids" for c in checks),
            "suggestion_ok": sum(c["ok"] for c in checks if c["check"] == "acceptable_recipe_ids"),
            "db_change_checks": sum(c["check"].startswith("db:") for c in checks),
            "db_change_ok": sum(c["ok"] for c in checks if c["check"].startswith("db:")),
            "per_category": dict(Counter(r["category"] for r in results)),
            "tolerance": {"kcal": KCAL_TOLERANCE, "grams": GRAM_TOLERANCE},
        },
        "failures": [{"id": r["id"], **c} for r in results for c in r["checks"] if not c["ok"]],
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    parser.add_argument("--out", default=str(EVALUATION_DIR / "runs" / "week8_tools.json"))
    args = parser.parse_args()
    report = anyio.run(run, args.split)
    if args.out != "-":
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    s = report["summary"]
    print(f"{report['cases']} {args.split} cases: numeric {s['numeric_ok']}/{s['numeric_checks']}, "
          f"suggestions {s['suggestion_ok']}/{s['suggestion_checks']}, "
          f"db changes {s['db_change_ok']}/{s['db_change_checks']}")
    for failure in report["failures"]:
        print("FAIL", failure)
    raise SystemExit(1 if report["failures"] else 0)


if __name__ == "__main__":
    main()
