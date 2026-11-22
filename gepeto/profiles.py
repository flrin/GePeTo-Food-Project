"""Profiles: the form-based setup, target storage and confirmed preference changes."""

from __future__ import annotations

import json
import re
import sqlite3

from .targets import GOAL_FACTORS, MAX_ACTIVITY, MIN_ACTIVITY, MissingInputs, estimate_targets

TARGET_FIELDS = ("kcal_target", "protein_target_g", "carbs_target_g", "fat_target_g")
TARGET_LIMITS = {"kcal_target": (500, 10000), "protein_target_g": (0, 1000),
                 "carbs_target_g": (0, 2000), "fat_target_g": (0, 1000)}
LIST_FIELDS = ("preferences", "restrictions")
BODY_FIELDS = ("sex", "age", "height_cm", "weight_kg", "activity_factor", "goal")
DIETS = ("vegan", "vegetarian", "pescatarian")  # strictest first
EXCLUDE_PATTERN = re.compile(r"^(?:no|dislikes?|avoid|allergic to|not)\s+(.+)$")
MAX_LIST_ITEM = 80


class ProfileError(ValueError):
    pass


class ProfileNotFound(LookupError):
    pass


def _row_to_profile(row: sqlite3.Row) -> dict:
    profile = dict(row)
    for name in LIST_FIELDS:
        profile[name] = json.loads(profile[name])
    return profile


def get_profile(conn: sqlite3.Connection, profile_id: int) -> dict:
    row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    if row is None:
        raise ProfileNotFound(f"no profile with id {profile_id}")
    return _row_to_profile(row)


def list_profiles(conn: sqlite3.Connection) -> list[dict]:
    return [_row_to_profile(row) for row in conn.execute("SELECT * FROM profiles ORDER BY id")]


def _clean_list(values) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise ProfileError("preferences and restrictions must be lists of short texts")
    cleaned: list[str] = []
    for value in values:
        text = " ".join(str(value).split()).lower()
        if not text:
            continue
        if len(text) > MAX_LIST_ITEM:
            raise ProfileError(f"'{text[:20]}...' is longer than {MAX_LIST_ITEM} characters")
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _number(data: dict, key: str, low: float, high: float, integer: bool = False):
    value = data.get(key)
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(f"{key} must be a number")
    if not low <= value <= high:
        raise ProfileError(f"{key} must be between {low} and {high}")
    return int(value) if integer else float(value)


def validate_profile(data: dict) -> dict:
    """Return a cleaned copy of the form data or raise ProfileError."""
    name = " ".join(str(data.get("name") or "").split())
    if not name:
        raise ProfileError("name is required")
    sex = data.get("sex") or None
    if sex not in (None, "female", "male"):
        raise ProfileError("sex must be 'female', 'male' or empty")
    goal = data.get("goal") or None
    if goal not in (None, *GOAL_FACTORS):
        raise ProfileError(f"goal must be one of {sorted(GOAL_FACTORS)} or empty")
    clean = {
        "name": name[:80],
        "sex": sex,
        "age": _number(data, "age", 1, 120, integer=True),
        "height_cm": _number(data, "height_cm", 50, 260),
        "weight_kg": _number(data, "weight_kg", 20, 400),
        "activity_factor": _number(data, "activity_factor", MIN_ACTIVITY, MAX_ACTIVITY),
        "goal": goal,
        "preferences": _clean_list(data.get("preferences")),
        "restrictions": _clean_list(data.get("restrictions")),
    }
    for key in TARGET_FIELDS:
        clean[key] = _number(data, key, *TARGET_LIMITS[key])
    clean["target_source"] = _target_source(clean)
    return clean


def _target_source(profile: dict) -> str | None:
    """'estimate' if the stored targets are exactly the formula result, else 'manual'."""
    if all(profile[key] is None for key in TARGET_FIELDS):
        return None
    try:
        estimate = estimate_targets(profile).as_dict()
    except (MissingInputs, ValueError):
        return "manual"
    same = all(profile[key] is not None and abs(profile[key] - estimate[key]) < 0.5 for key in TARGET_FIELDS)
    return "estimate" if same else "manual"


def save_profile(conn: sqlite3.Connection, data: dict, profile_id: int | None = None) -> dict:
    clean = validate_profile(data)
    fields = ["name", *BODY_FIELDS, *TARGET_FIELDS, "target_source"]
    values = [clean[key] for key in fields] + [json.dumps(clean[k]) for k in LIST_FIELDS]
    fields += list(LIST_FIELDS)
    if profile_id is None:
        cursor = conn.execute(
            f"INSERT INTO profiles ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})", values
        )
        profile_id = cursor.lastrowid
    else:
        get_profile(conn, profile_id)
        conn.execute(
            f"UPDATE profiles SET {', '.join(f'{key} = ?' for key in fields)} WHERE id = ?",
            (*values, profile_id),
        )
    conn.commit()
    return get_profile(conn, profile_id)


# ---------- confirmed changes coming from the chat (update_preferences) ----------

def normalize_change(change: dict) -> dict:
    """Validate {field, op, value}; the same shape the golden dataset uses."""
    field, op, value = change.get("field"), change.get("op"), change.get("value")
    if field in LIST_FIELDS:
        if op not in ("add", "remove"):
            raise ProfileError(f"{field} supports 'add' or 'remove'")
        text = " ".join(str(value or "").split()).lower()
        if not text or len(text) > MAX_LIST_ITEM:
            raise ProfileError(f"value must be 1-{MAX_LIST_ITEM} characters")
        return {"field": field, "op": op, "value": text}
    if field in TARGET_FIELDS:
        if op != "set":
            raise ProfileError(f"{field} only supports 'set'")
        number = _number({"v": value}, "v", *TARGET_LIMITS[field])
        if number is None:
            raise ProfileError(f"{field} needs a number")
        return {"field": field, "op": op, "value": number}
    raise ProfileError(f"field must be one of {[*LIST_FIELDS, *TARGET_FIELDS]}")


def apply_change(conn: sqlite3.Connection, profile_id: int, change: dict) -> dict:
    """Apply an already confirmed change. Repeating it is harmless (changed=False)."""
    change = normalize_change(change)
    profile = get_profile(conn, profile_id)
    field, op, value = change["field"], change["op"], change["value"]
    before = profile[field]
    if field in LIST_FIELDS:
        items = list(before)
        if op == "add" and value not in items:
            items.append(value)
        elif op == "remove" and value in items:
            items.remove(value)
        after = items
        conn.execute(f"UPDATE profiles SET {field} = ? WHERE id = ?", (json.dumps(after), profile_id))
    else:
        after = value
        conn.execute(
            f"UPDATE profiles SET {field} = ?, target_source = 'manual' WHERE id = ?", (after, profile_id)
        )
    conn.commit()
    return {"change": change, "changed": before != after, "before": before, "after": after}


# ---------- what the restrictions and preferences mean for food choices ----------

def dietary_rules(profile: dict) -> dict:
    """Turn free-text restrictions/preferences into filters we can actually check.

    Anything we do not understand is reported back as `unapplied`, so the app can
    say it could not check it instead of pretending it did.
    """
    diets: list[str] = []
    exclude: list[str] = []
    unapplied: list[str] = []
    for text in profile.get("restrictions", []) + profile.get("preferences", []):
        if text in DIETS:
            diets.append(text)
            continue
        match = EXCLUDE_PATTERN.match(text)
        if match:
            exclude.append(match.group(1).strip())
        elif text.startswith("likes "):
            continue  # a positive preference, nothing to filter
        else:
            unapplied.append(text)
    return {"diets": diets, "exclude": exclude, "unapplied": unapplied}


def term_variants(term: str) -> set[str]:
    """'mushrooms' should also match the ingredient 'mushroom'."""
    term = term.lower().strip()
    variants = {term}
    if term.endswith("es") and len(term) > 4:
        variants.add(term[:-2])
    if term.endswith("s") and len(term) > 3:
        variants.add(term[:-1])
    return variants


def mentions(text: str, term: str) -> bool:
    text = text.lower()
    return any(re.search(rf"\b{re.escape(variant)}", text) for variant in term_variants(term))
