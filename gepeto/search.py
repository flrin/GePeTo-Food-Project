"""Week 8 search: a plain keyword match over SQLite plus deterministic recipe filters.

This is only a baseline so the form and the MCP tools work end to end. Week 9
replaces the ranking with hybrid retrieval (Chroma + BM25); the hard filters
(diet, excluded ingredients, kcal/protein limits) stay here, in Python.
"""

from __future__ import annotations

import json
import re
import sqlite3

from .nutrition import NUTRIENT_FIELDS, recipe_per_serving, rounded
from .profiles import DIETS, dietary_rules, mentions

STOP_WORDS = {"a", "an", "and", "the", "of", "with", "in", "g", "gram", "grams", "some", "for", "me", "my"}

# What a USDA category tells us about a single food. None = we can't tell.
CATEGORY_DIETS = {
    "Poultry Products": {"vegan": False, "vegetarian": False, "pescatarian": False},
    "Beef Products": {"vegan": False, "vegetarian": False, "pescatarian": False},
    "Pork Products": {"vegan": False, "vegetarian": False, "pescatarian": False},
    "Lamb, Veal, and Game Products": {"vegan": False, "vegetarian": False, "pescatarian": False},
    "Sausages and Luncheon Meats": {"vegan": False, "vegetarian": False, "pescatarian": False},
    "Finfish and Shellfish Products": {"vegan": False, "vegetarian": False, "pescatarian": True},
    "Dairy and Egg Products": {"vegan": False, "vegetarian": True, "pescatarian": True},
    "Vegetables and Vegetable Products": {"vegan": True, "vegetarian": True, "pescatarian": True},
    "Fruits and Fruit Juices": {"vegan": True, "vegetarian": True, "pescatarian": True},
    "Legumes and Legume Products": {"vegan": True, "vegetarian": True, "pescatarian": True},
    "Nut and Seed Products": {"vegan": True, "vegetarian": True, "pescatarian": True},
    "Cereal Grains and Pasta": {"vegan": True, "vegetarian": True, "pescatarian": True},
    "Spices and Herbs": {"vegan": True, "vegetarian": True, "pescatarian": True},
}
RECIPE_DIET_TAGS = {
    "vegan": {"vegan"},
    "vegetarian": {"vegetarian", "vegan"},
    "pescatarian": {"pescatarian", "vegetarian", "vegan"},
}


def query_terms(query: str) -> list[str]:
    words = re.findall(r"[a-z]+", query.lower())
    return [word for word in words if len(word) > 1 and word not in STOP_WORDS]


def source_ref(source: str, source_id: str) -> str:
    return f"usda:{source_id}" if source == "usda_sr_legacy" else f"{source}:{source_id}"


def food_card(row: sqlite3.Row) -> dict:
    return {
        "food_id": row["id"],
        "source_id": source_ref(row["source"], row["source_id"]),
        "description": row["description"],
        "category": row["category"],
        "prep_state": row["prep_state"],
        "per_100g": {name: row[name] for name in NUTRIENT_FIELDS},
        "unknown_fields": [name for name in NUTRIENT_FIELDS if row[name] is None],
        "version": row["version"],
        "user_entered": bool(row["user_entered"]),
    }


def get_food(conn: sqlite3.Connection, food_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM foods WHERE id = ?", (food_id,)).fetchone()


def search_foods(
    conn: sqlite3.Connection, query: str, prep_state: str | None = None,
    category: str | None = None, limit: int = 10,
) -> list[dict]:
    terms = query_terms(query)
    if not terms:
        return []
    scored = []
    for row in conn.execute("SELECT * FROM foods ORDER BY id"):
        if prep_state and row["prep_state"] != prep_state:
            continue
        if category and (row["category"] or "").lower() != category.lower():
            continue
        text = f"{row['description']} {row['category'] or ''}"
        matched = [term for term in terms if mentions(text, term)]
        if matched:
            # More query words matched first, then the shortest (most generic) description.
            scored.append((-len(matched), len(row["description"]), row["id"], row, matched))
    scored.sort(key=lambda item: item[:3])
    results = []
    for *_, row, matched in scored[:limit]:
        card = food_card(row)
        card["matched_terms"] = matched
        results.append(card)
    return results


def food_diet_status(row: sqlite3.Row, diet: str) -> bool | None:
    return CATEGORY_DIETS.get(row["category"] or "", {}).get(diet)


def recipe_ingredients(conn: sqlite3.Connection, recipe_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT ri.food_id, ri.grams, ri.name, f.description, f.source, f.source_id, f.version
           FROM recipe_ingredients ri JOIN foods f ON f.id = ri.food_id
           WHERE ri.recipe_id = ? ORDER BY ri.rowid""",
        (recipe_id,),
    ).fetchall()
    return [
        {
            "name": row["name"] or row["description"],
            "food_id": row["food_id"],
            "source_id": source_ref(row["source"], row["source_id"]),
            "description": row["description"],
            "grams": row["grams"],
            "food_version": row["version"],
        }
        for row in rows
    ]


def recipe_card(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    ingredients = recipe_ingredients(conn, row["id"])
    per_serving = recipe_per_serving(conn, row["id"])
    return {
        "recipe_id": row["id"],
        "source_id": f"recipe:{row['id']}",
        "title": row["title"],
        "meal_type": row["meal_type"],
        "servings": row["servings"],
        "weight_state": row["weight_state"],
        "cooked_weight_g": row["cooked_weight_g"],
        "tags": json.loads(row["tags"]),
        "ingredients": [{"name": i["name"], "source_id": i["source_id"], "grams": i["grams"]} for i in ingredients],
        "per_serving": rounded({name: per_serving[name] for name in NUTRIENT_FIELDS}),
        "estimate": True,
    }


def recipe_violations(conn: sqlite3.Connection, recipe_id: str, tags: list[str],
                      diets: list[str], exclude: list[str]) -> list[str]:
    problems = []
    for diet in diets:
        if not set(tags) & RECIPE_DIET_TAGS[diet]:
            problems.append(f"not {diet}")
    texts = [f"{i['name']} {i['description']}" for i in recipe_ingredients(conn, recipe_id)]
    for term in exclude:
        if any(mentions(text, term) for text in texts):
            problems.append(f"contains {term}")
    return problems


def search_recipes(
    conn: sqlite3.Connection,
    profile: dict,
    query: str | None = None,
    meal_type: str | None = None,
    diet: str | None = None,
    require_tag: str | None = None,
    exclude_ingredients: list[str] | None = None,
    max_kcal: float | None = None,
    min_protein_g: float | None = None,
    limit: int = 5,
) -> dict:
    """Recipes that pass every hard rule. The profile's own restrictions always apply."""
    rules = dietary_rules(profile)
    diets = list(dict.fromkeys(rules["diets"] + ([diet] if diet else [])))
    unknown_diets = [d for d in diets if d not in DIETS]
    diets = [d for d in diets if d in DIETS]
    exclude = list(dict.fromkeys(rules["exclude"] + [t.lower().strip() for t in exclude_ingredients or [] if t.strip()]))
    terms = query_terms(query or "")

    matches, excluded = [], 0
    for row in conn.execute("SELECT * FROM recipes ORDER BY id"):
        tags = json.loads(row["tags"])
        if meal_type and row["meal_type"] != meal_type:
            continue
        if require_tag and require_tag not in tags:
            continue
        if recipe_violations(conn, row["id"], tags, diets, exclude):
            excluded += 1
            continue
        per = recipe_per_serving(conn, row["id"])
        if max_kcal is not None and (per["kcal"] is None or per["kcal"] > max_kcal):
            continue
        if min_protein_g is not None and (per["protein_g"] is None or per["protein_g"] < min_protein_g):
            continue
        card = recipe_card(conn, row)
        text = " ".join([row["title"], row["meal_type"], *tags, *(i["name"] for i in card["ingredients"])])
        card["matched_terms"] = [term for term in terms if mentions(text, term)]
        matches.append(card)
    if terms:
        matches.sort(key=lambda card: -len(card["matched_terms"]))  # stable: ties keep id order
    return {
        "results": matches[:limit],
        "total_matches": len(matches),
        "applied_rules": {
            "diets": diets, "exclude_ingredients": exclude, "meal_type": meal_type,
            "require_tag": require_tag, "max_kcal": max_kcal, "min_protein_g": min_protein_g,
        },
        "excluded_by_restrictions": excluded,
        "unapplied_restrictions": rules["unapplied"] + unknown_diets,
        "note": "Nutrients are per serving, estimated from raw ingredient weights. "
                "No allergen data: an ingredient list without a term is not proof it is absent.",
    }

