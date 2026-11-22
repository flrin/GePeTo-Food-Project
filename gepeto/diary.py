"""Food diary: preview a meal, save a confirmed preview once, summarize a day.

The flow is the same for the form and (from week 10) the chat:

    calculate_meal  -> preview (numbers + rule checks), stored in meal_previews
    user confirms   -> the application mints a confirmation token
    log_meal        -> copies the preview into meal_logs exactly once

Meal logs keep the nutrient snapshot shown in the preview and the food version,
so a later import can't change the totals of earlier days.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone

from .errors import GepetoError
from .nutrition import NUTRIENT_FIELDS, add_up, recipe_per_serving, rounded, scale
from .profiles import TARGET_FIELDS, dietary_rules, get_profile, mentions
from .search import food_diet_status, recipe_violations, source_ref

MAX_ITEMS = 20
MAX_GRAMS = 5000
MAX_SERVINGS = 20
PREVIEW_TTL = timedelta(days=1)
TARGET_FOR = dict(zip(NUTRIENT_FIELDS, TARGET_FIELDS))
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value: str | None) -> str:
    if value is None:
        return date.today().isoformat()
    if not isinstance(value, str) or not DATE_PATTERN.match(value):
        raise GepetoError("invalid_input", f"date must look like YYYY-MM-DD, got {value!r}")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise GepetoError("invalid_input", f"{value!r} is not a real date") from None


def _positive(value, name: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GepetoError("invalid_input", f"{name} must be a number")
    if not 0 < value <= maximum:
        raise GepetoError("invalid_input", f"{name} must be more than 0 and at most {maximum}")
    return float(value)


def recipe_fingerprint(conn: sqlite3.Connection, recipe_id: str) -> str:
    """Changes when the recipe or any ingredient's food record changes."""
    recipe = conn.execute("SELECT content_hash FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    versions = conn.execute(
        """SELECT ri.food_id, f.version FROM recipe_ingredients ri JOIN foods f ON f.id = ri.food_id
           WHERE ri.recipe_id = ? ORDER BY ri.food_id""",
        (recipe_id,),
    ).fetchall()
    payload = json.dumps([recipe["content_hash"], [tuple(row) for row in versions]])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def resolve_item(conn: sqlite3.Connection, item: dict, rules: dict) -> dict:
    """One requested item -> label, quantities, nutrient snapshot and rule checks."""
    food_id, recipe_id = item.get("food_id"), item.get("recipe_id")
    grams, servings = item.get("grams"), item.get("servings")
    if (food_id is None) == (recipe_id is None):
        raise GepetoError("invalid_input", "each item needs exactly one of food_id or recipe_id")

    if food_id is not None:
        if servings is not None or grams is None:
            raise GepetoError("invalid_input", "a food item needs grams (servings are only for recipes)")
        grams = _positive(grams, "grams", MAX_GRAMS)
        row = conn.execute("SELECT * FROM foods WHERE id = ?", (food_id,)).fetchone()
        if row is None:
            raise GepetoError("not_found", f"no food with id {food_id}")
        nutrients = scale(dict(row), grams)
        checks = []
        for diet in rules["diets"]:
            ok = food_diet_status(row, diet)
            checks.append(_check(f"diet:{diet}", ok, f"{row['category'] or 'unknown category'}"))
        for term in rules["exclude"]:
            checks.append(_check(f"exclude:{term}", not mentions(row["description"], term), row["description"]))
        return {
            "kind": "food", "food_id": row["id"], "recipe_id": None,
            "label": row["description"], "source_id": source_ref(row["source"], row["source_id"]),
            "prep_state": row["prep_state"], "grams": grams, "servings": None,
            "food_version": row["version"], "fingerprint": str(row["version"]),
            "nutrients": nutrients, "estimate": False, "checks": checks,
        }

    row = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if row is None:
        raise GepetoError("not_found", f"no recipe with id {recipe_id!r}")
    if (grams is None) == (servings is None):
        raise GepetoError("invalid_input", "a recipe item needs either servings or cooked grams, not both")
    per_serving = recipe_per_serving(conn, recipe_id)
    if servings is not None:
        servings = _positive(servings, "servings", MAX_SERVINGS)
    else:
        grams = _positive(grams, "grams", MAX_GRAMS)
        if not row["cooked_weight_g"]:
            raise GepetoError(
                "missing_data",
                f"recipe {recipe_id} has no cooked weight, so it can only be logged by servings",
            )
        servings = grams / row["cooked_weight_g"] * row["servings"]
    nutrients = {
        name: None if per_serving[name] is None else per_serving[name] * servings for name in NUTRIENT_FIELDS
    }
    tags = json.loads(row["tags"])
    checks = []
    for diet in rules["diets"]:
        problems = recipe_violations(conn, recipe_id, tags, [diet], [])
        checks.append(_check(f"diet:{diet}", not problems, ", ".join(tags) or "no diet tags"))
    for term in rules["exclude"]:
        problems = recipe_violations(conn, recipe_id, tags, [], [term])
        checks.append(_check(f"exclude:{term}", not problems, "ingredient list"))
    return {
        "kind": "recipe", "food_id": None, "recipe_id": recipe_id,
        "label": row["title"], "source_id": f"recipe:{recipe_id}", "prep_state": None,
        "grams": grams, "servings": servings, "food_version": None,
        "fingerprint": recipe_fingerprint(conn, recipe_id),
        "nutrients": nutrients, "estimate": True, "checks": checks,
    }


def _check(rule: str, ok: bool | None, detail: str) -> dict:
    status = "unknown" if ok is None else ("pass" if ok else "fail")
    return {"rule": rule, "status": status, "detail": detail}


def _day_totals(conn: sqlite3.Connection, profile_id: int, log_date: str) -> dict:
    rows = conn.execute(
        f"SELECT {', '.join(NUTRIENT_FIELDS)} FROM meal_logs WHERE profile_id = ? AND log_date = ?",
        (profile_id, log_date),
    ).fetchall()
    return add_up([dict(row) for row in rows]) if rows else {name: 0.0 for name in NUTRIENT_FIELDS}


def _remaining(profile: dict, totals: dict) -> dict:
    return {
        name: None if profile[TARGET_FOR[name]] is None or totals[name] is None
        else profile[TARGET_FOR[name]] - totals[name]
        for name in NUTRIENT_FIELDS
    }


def preview_meal(conn: sqlite3.Connection, profile_id: int, items: list[dict], log_date: str | None = None) -> dict:
    """Calculate a meal and store it as a preview. meal_logs is not touched."""
    log_date = parse_date(log_date)
    if not isinstance(items, list) or not items:
        raise GepetoError("invalid_input", "give at least one item")
    if len(items) > MAX_ITEMS:
        raise GepetoError("invalid_input", f"at most {MAX_ITEMS} items per meal")
    profile = get_profile(conn, profile_id)
    rules = dietary_rules(profile)
    resolved = [resolve_item(conn, item, rules) for item in items]
    totals = add_up([item["nutrients"] for item in resolved])

    logged = _day_totals(conn, profile_id, log_date)
    after = add_up([logged, totals])
    remaining_after = _remaining(profile, after)
    kcal_target = profile["kcal_target"]
    day_checks = [_check(
        "daily_kcal_target",
        None if kcal_target is None or after["kcal"] is None else after["kcal"] <= kcal_target,
        "no kcal target set" if kcal_target is None else f"day total after this meal vs target {kcal_target:g} kcal",
    )]

    preview_id = "pv_" + uuid.uuid4().hex[:16]
    cutoff = (datetime.now(timezone.utc) - PREVIEW_TTL).isoformat(timespec="seconds")
    conn.execute("DELETE FROM meal_previews WHERE logged_request_id IS NULL AND created_at < ?", (cutoff,))
    conn.execute(
        "INSERT INTO meal_previews (id, profile_id, log_date, items, created_at) VALUES (?, ?, ?, ?, ?)",
        (preview_id, profile_id, log_date, json.dumps(resolved), now()),
    )
    conn.commit()
    return {
        "preview_id": preview_id,
        "date": log_date,
        "items": [_public_item(item) for item in resolved],
        "totals": rounded(totals),
        "unknown_fields": [name for name in NUTRIENT_FIELDS if totals[name] is None],
        "estimate": any(item["estimate"] for item in resolved),
        "day_before": rounded(logged),
        "remaining_after": rounded(remaining_after),
        "checks": [check for item in resolved for check in item["checks"]] + day_checks,
        "unapplied_restrictions": rules["unapplied"],
        "logged": False,
    }


def _public_item(item: dict) -> dict:
    return {
        "kind": item["kind"], "food_id": item["food_id"], "recipe_id": item["recipe_id"],
        "label": item["label"], "source_id": item["source_id"], "prep_state": item["prep_state"],
        "grams": item["grams"], "servings": None if item["servings"] is None else round(item["servings"], 3),
        "nutrients": rounded(item["nutrients"]),
        "unknown_fields": [name for name in NUTRIENT_FIELDS if item["nutrients"][name] is None],
        "estimate": item["estimate"], "checks": item["checks"],
    }


def _current_fingerprint(conn: sqlite3.Connection, item: dict) -> str | None:
    if item["kind"] == "food":
        row = conn.execute("SELECT version FROM foods WHERE id = ?", (item["food_id"],)).fetchone()
        return None if row is None else str(row["version"])
    exists = conn.execute("SELECT 1 FROM recipes WHERE id = ?", (item["recipe_id"],)).fetchone()
    return None if exists is None else recipe_fingerprint(conn, item["recipe_id"])


def _logged_meal(conn: sqlite3.Connection, request_id: str) -> dict | None:
    rows = conn.execute(
        "SELECT * FROM meal_logs WHERE request_id = ? ORDER BY item_index", (request_id,)
    ).fetchall()
    if not rows:
        return None
    return {
        "profile_id": rows[0]["profile_id"], "preview_id": rows[0]["preview_id"],
        "date": rows[0]["log_date"], "entry_ids": [row["id"] for row in rows],
        "totals": add_up([{name: row[name] for name in NUTRIENT_FIELDS} for row in rows]),
    }


def _result(status: str, request_id: str, meal: dict) -> dict:
    return {
        "status": status, "request_id": request_id, "preview_id": meal["preview_id"],
        "date": meal["date"], "entry_ids": meal["entry_ids"], "totals": rounded(meal["totals"]),
    }


def validate_request_id(request_id) -> str:
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,80}", request_id):
        raise GepetoError("invalid_input", "request_id must be 8-80 characters of letters, digits, _ . : -")
    return request_id


def log_preview(conn: sqlite3.Connection, profile_id: int, preview_id: str, request_id: str) -> dict:
    """Save a (confirmed) preview. Safe to repeat: the second call changes nothing.

    - same request_id again           -> status 'already_logged', no new rows
    - same preview, new request_id     -> status 'already_logged' (a preview is saved once)
    - request_id used for another meal -> conflict error
    """
    validate_request_id(request_id)
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")  # one writer at a time, so the checks below can't race
    try:
        meal = _logged_meal(conn, request_id)
        if meal is not None:
            conn.rollback()
            if meal["profile_id"] == profile_id and meal["preview_id"] == preview_id:
                return _result("already_logged", request_id, meal)
            raise GepetoError("conflict", f"request_id {request_id} was already used for a different meal")

        preview = conn.execute(
            "SELECT * FROM meal_previews WHERE id = ? AND profile_id = ?", (preview_id, profile_id)
        ).fetchone()
        if preview is None:
            raise GepetoError("not_found", f"no preview {preview_id!r} for this profile")
        if preview["logged_request_id"] is not None:
            conn.rollback()
            first = preview["logged_request_id"]
            return _result("already_logged", first, _logged_meal(conn, first))

        items = json.loads(preview["items"])
        changed = [item["label"] for item in items if _current_fingerprint(conn, item) != item["fingerprint"]]
        if changed:
            raise GepetoError(
                "stale_preview", "food data changed since the preview was shown, calculate again: " + "; ".join(changed)
            )
        created = now()
        for index, item in enumerate(items):
            conn.execute(
                f"""INSERT INTO meal_logs (profile_id, log_date, request_id, item_index, preview_id, label,
                        food_id, food_version, recipe_id, grams, servings, {', '.join(NUTRIENT_FIELDS)}, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {', '.join('?' * len(NUTRIENT_FIELDS))}, ?)""",
                (
                    profile_id, preview["log_date"], request_id, index, preview_id, item["label"],
                    item["food_id"], item["food_version"], item["recipe_id"], item["grams"], item["servings"],
                    *(item["nutrients"][name] for name in NUTRIENT_FIELDS), created,
                ),
            )
        conn.execute("UPDATE meal_previews SET logged_request_id = ? WHERE id = ?", (request_id, preview_id))
        conn.commit()
    except sqlite3.IntegrityError:
        # Only reachable if another writer slipped past BEGIN IMMEDIATE (e.g. another process
        # with a different locking setup). The UNIQUE constraint still keeps one copy.
        conn.rollback()
        meal = _logged_meal(conn, request_id)
        if meal is not None and meal["preview_id"] == preview_id:
            return _result("already_logged", request_id, meal)
        raise
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    return _result("logged", request_id, _logged_meal(conn, request_id))


def daily_summary(conn: sqlite3.Connection, profile_id: int, log_date: str | None = None) -> dict:
    log_date = parse_date(log_date)
    profile = get_profile(conn, profile_id)
    rows = conn.execute(
        "SELECT * FROM meal_logs WHERE profile_id = ? AND log_date = ? ORDER BY created_at, request_id, item_index",
        (profile_id, log_date),
    ).fetchall()
    meals: dict[str, dict] = {}
    for row in rows:
        meal = meals.setdefault(row["request_id"], {"request_id": row["request_id"], "created_at": row["created_at"], "items": []})
        nutrients = {name: row[name] for name in NUTRIENT_FIELDS}
        meal["items"].append({
            "entry_id": row["id"], "label": row["label"],
            "source_id": f"recipe:{row['recipe_id']}" if row["recipe_id"] else _food_ref(conn, row["food_id"]),
            "grams": row["grams"], "servings": row["servings"],
            "nutrients": rounded(nutrients), "_raw": nutrients,
        })
    for meal in meals.values():
        meal["totals"] = rounded(add_up([item["_raw"] for item in meal["items"]]))
        for item in meal["items"]:
            del item["_raw"]
    totals = _day_totals(conn, profile_id, log_date)
    targets = {name: profile[TARGET_FOR[name]] for name in NUTRIENT_FIELDS}
    return {
        "date": log_date,
        "meals": list(meals.values()),
        "entry_count": len(rows),
        "totals": rounded(totals),
        "targets": targets,
        "target_source": profile["target_source"],
        "remaining": rounded(_remaining(profile, totals)),
        "unknown_fields": [name for name in NUTRIENT_FIELDS if totals[name] is None],
    }


def _food_ref(conn: sqlite3.Connection, food_id: int) -> str:
    row = conn.execute("SELECT source, source_id FROM foods WHERE id = ?", (food_id,)).fetchone()
    return "unknown" if row is None else source_ref(row["source"], row["source_id"])


def delete_meal(conn: sqlite3.Connection, profile_id: int, request_id: str) -> int:
    """Remove a logged meal (the form's 'delete' button). Not exposed to the model."""
    cursor = conn.execute(
        "DELETE FROM meal_logs WHERE profile_id = ? AND request_id = ?", (profile_id, request_id)
    )
    conn.commit()
    if cursor.rowcount == 0:
        raise GepetoError("not_found", f"no logged meal {request_id!r}")
    return cursor.rowcount

