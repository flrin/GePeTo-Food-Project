"""Deterministic nutrient arithmetic. The language model never does these sums."""

from __future__ import annotations

import sqlite3

from .importer import NUTRIENT_FIELDS


def scale(per_100g: dict, grams: float) -> dict:
    return {
        name: None if per_100g.get(name) is None else per_100g[name] * grams / 100
        for name in NUTRIENT_FIELDS
    }


def add_up(parts: list[dict]) -> dict:
    """Sum nutrient dicts. If any part is unknown, the total is unknown (None), not a guess."""
    total: dict = {}
    for name in NUTRIENT_FIELDS:
        values = [part[name] for part in parts]
        total[name] = None if any(value is None for value in values) else sum(values)
    return total


def rounded(values: dict) -> dict:
    return {
        name: None if value is None else round(value, 0 if name == "kcal" else 1)
        for name, value in values.items()
    }


def food_nutrients(conn: sqlite3.Connection, food_id: int) -> dict:
    row = conn.execute(
        f"SELECT {', '.join(NUTRIENT_FIELDS)} FROM foods WHERE id = ?", (food_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown food id {food_id}")
    return dict(row)


def meal_totals(conn: sqlite3.Connection, items: list[tuple[int, float]]) -> dict:
    """items = [(food_id, grams), ...]"""
    return add_up([scale(food_nutrients(conn, food_id), grams) for food_id, grams in items])


def recipe_per_serving(conn: sqlite3.Connection, recipe_id: str) -> dict:
    """Estimated nutrients for one serving, calculated from the ingredient list."""
    recipe = conn.execute("SELECT servings FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if recipe is None:
        raise KeyError(f"unknown recipe {recipe_id}")
    items = conn.execute(
        "SELECT food_id, grams FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,)
    ).fetchall()
    total = meal_totals(conn, [(row["food_id"], row["grams"]) for row in items])
    per_serving = {
        name: None if value is None else value / recipe["servings"]
        for name, value in total.items()
    }
    return {**per_serving, "estimate": True}
