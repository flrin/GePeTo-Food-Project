"""The seven core tools as plain Python functions (Milestone 2, section 5).

`mcp_server.py` exposes exactly these over MCP. Keeping them here, without any
MCP types, means the numeric and duplicate-write tests can call them directly
and through the real MCP server and expect the same results.

The profile always comes from the ToolContext, which the trusted host fixes
when it starts the server. No tool takes a user or profile id as an argument.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import diary, profiles, search
from .confirm import Confirmations
from .db import connect
from .errors import GepetoError

TOOL_NAMES = (
    "get_profile", "get_daily_summary", "search_foods", "search_recipes",
    "calculate_meal", "log_meal", "update_preferences",
)
READ_ONLY_TOOLS = ("get_profile", "get_daily_summary", "search_foods", "search_recipes", "calculate_meal")


@dataclass(frozen=True)
class ToolContext:
    db_path: Path | str
    profile_id: int
    confirmations: Confirmations

    @contextmanager
    def db(self):
        try:
            conn = connect(self.db_path)
        except sqlite3.Error as exc:
            raise GepetoError("unavailable", f"database unavailable: {exc}") from exc
        try:
            yield conn
        except profiles.ProfileNotFound as exc:
            raise GepetoError("not_found", str(exc)) from exc
        except profiles.ProfileError as exc:
            raise GepetoError("invalid_input", str(exc)) from exc
        except sqlite3.OperationalError as exc:
            raise GepetoError("unavailable", f"database error: {exc}") from exc
        finally:
            conn.close()


def get_profile(ctx: ToolContext) -> dict:
    with ctx.db() as conn:
        profile = profiles.get_profile(conn, ctx.profile_id)
    # Only what the assistant needs: no age, weight or height.
    return {
        "name": profile["name"],
        "goal": profile["goal"],
        "targets": {key: profile[key] for key in profiles.TARGET_FIELDS},
        "target_source": profile["target_source"],
        "preferences": profile["preferences"],
        "restrictions": profile["restrictions"],
        "dietary_rules": profiles.dietary_rules(profile),
    }


def get_daily_summary(ctx: ToolContext, date: str) -> dict:
    with ctx.db() as conn:
        return diary.daily_summary(conn, ctx.profile_id, date)


def search_foods(ctx: ToolContext, query: str, prep_state: str | None = None, limit: int = 5) -> dict:
    if not search.query_terms(query):
        raise GepetoError("invalid_input", "query has no searchable words")
    with ctx.db() as conn:
        results = search.search_foods(conn, query, prep_state=prep_state, limit=limit)
    return {"results": results, "retrieval": "keyword_baseline_week8"}


def search_recipes(
    ctx: ToolContext, query: str | None = None, meal_type: str | None = None, diet: str | None = None,
    require_tag: str | None = None, exclude_ingredients: list[str] | None = None,
    max_kcal: float | None = None, min_protein_g: float | None = None,
    fit_remaining_kcal_on: str | None = None, limit: int = 5,
) -> dict:
    with ctx.db() as conn:
        profile = profiles.get_profile(conn, ctx.profile_id)
        if fit_remaining_kcal_on is not None:
            remaining = diary.daily_summary(conn, ctx.profile_id, fit_remaining_kcal_on)["remaining"]["kcal"]
            if remaining is None:
                raise GepetoError("missing_data", "no kcal target set, so 'fits the rest of my day' can't be checked")
            max_kcal = remaining if max_kcal is None else min(max_kcal, remaining)
        return search.search_recipes(
            conn, profile, query=query, meal_type=meal_type, diet=diet, require_tag=require_tag,
            exclude_ingredients=exclude_ingredients, max_kcal=max_kcal, min_protein_g=min_protein_g, limit=limit,
        )


def calculate_meal(ctx: ToolContext, items: list[dict], date: str | None = None) -> dict:
    with ctx.db() as conn:
        return diary.preview_meal(conn, ctx.profile_id, items, date)


def log_meal(ctx: ToolContext, preview_id: str, confirmation_token: str, request_id: str) -> dict:
    ctx.confirmations.check_meal(ctx.profile_id, preview_id, confirmation_token)
    with ctx.db() as conn:
        return diary.log_preview(conn, ctx.profile_id, preview_id, request_id)


def update_preferences(ctx: ToolContext, change: dict, confirmation_token: str) -> dict:
    try:
        change = profiles.normalize_change(change)
    except profiles.ProfileError as exc:
        raise GepetoError("invalid_input", str(exc)) from exc
    ctx.confirmations.check_preference(ctx.profile_id, change, confirmation_token)
    with ctx.db() as conn:
        return profiles.apply_change(conn, ctx.profile_id, change)
