"""GePeTo MCP tool server (official MCP Python SDK, stdio transport).

The host starts one server process per signed-in profile:

    GEPETO_DB=gepeto.db GEPETO_PROFILE_ID=1 GEPETO_CONFIRM_SECRET=<hex> python -m gepeto.mcp_server

The profile id and the confirmation secret come from that trusted environment,
never from tool arguments. Without a secret the read tools work and every write
is refused.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import tools
from .confirm import Confirmations
from .config import Settings
from .errors import GepetoError

PROFILE_ENV = "GEPETO_PROFILE_ID"
DatePattern = r"^\d{4}-\d{2}-\d{2}$"
LocalDate = Annotated[str, Field(pattern=DatePattern, description="Local date, YYYY-MM-DD.")]
NutrientName = Literal["kcal", "protein_g", "carbs_g", "fat_g"]


# ---------------------------------------------------------------- inputs

class MealItem(BaseModel):
    """One thing eaten: a food by grams, or a recipe by servings or by cooked grams."""

    model_config = ConfigDict(extra="forbid")
    food_id: int | None = Field(None, gt=0, description="food_id from search_foods.")
    recipe_id: str | None = Field(None, max_length=20, description="recipe_id from search_recipes.")
    grams: float | None = Field(None, gt=0, le=5000, description="Grams eaten. For a recipe: grams of the cooked dish.")
    servings: float | None = Field(None, gt=0, le=20, description="Recipe servings eaten (recipes only).")

    @model_validator(mode="after")
    def one_kind(self) -> "MealItem":
        if (self.food_id is None) == (self.recipe_id is None):
            raise ValueError("give exactly one of food_id or recipe_id")
        if self.food_id is not None and (self.grams is None or self.servings is not None):
            raise ValueError("a food needs grams, and servings only apply to recipes")
        if self.recipe_id is not None and (self.grams is None) == (self.servings is None):
            raise ValueError("a recipe needs either servings or cooked grams")
        return self


class PreferenceChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: Literal["preferences", "restrictions", "kcal_target", "protein_target_g", "carbs_target_g", "fat_target_g"]
    op: Literal["add", "remove", "set"] = Field(description="'add'/'remove' for lists, 'set' for targets.")
    value: str | float = Field(description="e.g. 'dislikes mushrooms', 'vegetarian', 'no shrimp', or 1900.")


# ---------------------------------------------------------------- outputs

class Nutrients(BaseModel):
    kcal: float | None
    protein_g: float | None
    carbs_g: float | None
    fat_g: float | None


class Check(BaseModel):
    rule: str
    status: Literal["pass", "fail", "unknown"]
    detail: str


class DietaryRules(BaseModel):
    diets: list[str]
    exclude: list[str]
    unapplied: list[str]


class ProfileResult(BaseModel):
    name: str
    goal: str | None
    targets: dict[str, float | None]
    target_source: Literal["estimate", "manual"] | None
    preferences: list[str]
    restrictions: list[str]
    dietary_rules: DietaryRules


class DiaryEntry(BaseModel):
    entry_id: int
    label: str | None
    source_id: str
    grams: float | None
    servings: float | None
    nutrients: Nutrients


class LoggedMeal(BaseModel):
    request_id: str
    created_at: str
    items: list[DiaryEntry]
    totals: Nutrients


class DailySummary(BaseModel):
    date: str
    meals: list[LoggedMeal]
    entry_count: int
    totals: Nutrients
    targets: Nutrients
    target_source: Literal["estimate", "manual"] | None
    remaining: Nutrients
    unknown_fields: list[NutrientName]


class MatchEvidence(BaseModel):
    method: Literal["hybrid", "dense", "bm25"]
    score: float = Field(description="Reciprocal rank fusion score: sum of 1/(60+rank) over the two lists.")
    dense_rank: int | None
    bm25_rank: int | None
    similarity: float | None = Field(description="Cosine similarity of the best matching chunk.")
    bm25_score: float | None
    matched_terms: list[str]
    chunk_ids: list[str]


class RetrievalInfo(BaseModel):
    method: Literal["hybrid", "keyword_fallback", "filters_only"]
    index_version: str | None
    index_up_to_date: bool | None = Field(description="False: records changed after the index was built.")
    reason: str | None = Field(description="Why hybrid retrieval was not used, if it wasn't.")
    unmapped_records: list[str] = []
    recheck_dropped: list[str] = []


class FoodResult(BaseModel):
    food_id: int
    source_id: str
    description: str
    category: str | None
    prep_state: Literal["raw", "cooked", "unspecified"]
    per_100g: Nutrients
    unknown_fields: list[NutrientName]
    version: int
    user_entered: bool
    matched_terms: list[str]
    match: MatchEvidence | None


class FoodSearch(BaseModel):
    results: list[FoodResult]
    retrieval: RetrievalInfo


class Ingredient(BaseModel):
    name: str
    source_id: str
    grams: float


class RecipeResult(BaseModel):
    recipe_id: str
    source_id: str
    title: str
    meal_type: str
    servings: int
    weight_state: Literal["raw", "cooked"]
    cooked_weight_g: float | None
    tags: list[str]
    ingredients: list[Ingredient]
    per_serving: Nutrients
    estimate: bool
    matched_terms: list[str]
    match: MatchEvidence | None


class RecipeSearch(BaseModel):
    results: list[RecipeResult]
    retrieval: RetrievalInfo
    total_matches: int
    applied_rules: dict[str, Any]
    excluded_by_restrictions: int
    unapplied_restrictions: list[str]
    note: str


class PreviewItem(BaseModel):
    kind: Literal["food", "recipe"]
    food_id: int | None
    recipe_id: str | None
    label: str
    source_id: str
    prep_state: str | None
    grams: float | None
    servings: float | None
    nutrients: Nutrients
    unknown_fields: list[NutrientName]
    estimate: bool
    checks: list[Check]


class MealPreview(BaseModel):
    preview_id: str
    date: str
    items: list[PreviewItem]
    totals: Nutrients
    unknown_fields: list[NutrientName]
    estimate: bool
    day_before: Nutrients
    remaining_after: Nutrients
    checks: list[Check]
    unapplied_restrictions: list[str]
    logged: bool


class LogResult(BaseModel):
    status: Literal["logged", "already_logged"]
    request_id: str
    preview_id: str | None
    date: str
    entry_ids: list[int]
    totals: Nutrients


class PreferenceResult(BaseModel):
    change: dict[str, Any]
    changed: bool
    before: list[str] | float | None
    after: list[str] | float | None


# ---------------------------------------------------------------- server

INSTRUCTIONS = """Nutrition tools for one signed-in user. All numbers are computed in Python
from stored records; repeat them, do not recalculate. Cite source_id values.
calculate_meal only previews. log_meal and update_preferences need a confirmation
token that only the application can issue after the user confirms."""

READ = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except GepetoError as exc:
        raise ToolError(str(exc)) from exc


def build_server(ctx: tools.ToolContext) -> MCPServer:
    server = MCPServer("gepeto", instructions=INSTRUCTIONS, version="0.3.0")

    @server.tool(annotations=READ)
    def get_profile() -> ProfileResult:
        """The user's daily targets, preferences and restrictions (no body measurements)."""
        return ProfileResult(**_run(tools.get_profile, ctx))

    @server.tool(annotations=READ)
    def get_daily_summary(date: LocalDate) -> DailySummary:
        """Logged meals for one day, their totals, the targets and what is left. Unknown values are null, never 0."""
        return DailySummary(**_run(tools.get_daily_summary, ctx, date))

    @server.tool(annotations=READ)
    def search_foods(
        query: Annotated[str, Field(min_length=1, max_length=200, description="Food name, e.g. 'raw chicken breast'.")],
        prep_state: Annotated[Literal["raw", "cooked", "unspecified"] | None,
                              Field(description="Only foods in this state.")] = None,
        limit: Annotated[int, Field(ge=1, le=20)] = 5,
    ) -> FoodSearch:
        """Find food records (values per 100 g), ranked by hybrid retrieval. Raw and cooked versions are
        separate records. The best match may still be the wrong food: check the description."""
        return FoodSearch(**_run(tools.search_foods, ctx, query, prep_state, limit))

    @server.tool(annotations=READ)
    def search_recipes(
        query: Annotated[str | None, Field(max_length=200, description="Words to rank by, e.g. 'lentil soup'.")] = None,
        meal_type: Literal["breakfast", "lunch", "dinner", "snack"] | None = None,
        diet: Annotated[Literal["vegetarian", "vegan", "pescatarian"] | None,
                        Field(description="Extra diet rule; the profile's own restrictions always apply.")] = None,
        require_tag: Annotated[str | None, Field(max_length=40, description="e.g. 'pescatarian' for fish/seafood dishes.")] = None,
        exclude_ingredients: Annotated[list[Annotated[str, Field(min_length=1, max_length=40)]] | None,
                                       Field(max_length=10, description="Ingredient words to avoid.")] = None,
        max_kcal: Annotated[float | None, Field(gt=0, le=5000, description="Per serving.")] = None,
        min_protein_g: Annotated[float | None, Field(ge=0, le=500, description="Per serving.")] = None,
        fit_remaining_kcal_on: Annotated[str | None, Field(pattern=DatePattern,
                                         description="Also require kcal <= what is left on this date.")] = None,
        limit: Annotated[int, Field(ge=1, le=10)] = 5,
    ) -> RecipeSearch:
        """Recipes that pass every hard rule (diet, excluded ingredients, kcal/protein limits), ranked by the
        query with hybrid retrieval. Per-serving estimates."""
        return RecipeSearch(**_run(
            tools.search_recipes, ctx, query, meal_type, diet, require_tag, exclude_ingredients,
            max_kcal, min_protein_g, fit_remaining_kcal_on, limit,
        ))

    @server.tool(annotations=READ)
    def calculate_meal(
        items: Annotated[list[MealItem], Field(min_length=1, max_length=20)],
        date: Annotated[str | None, Field(pattern=DatePattern, description="Day the meal belongs to; default today.")] = None,
    ) -> MealPreview:
        """Calories and macros for a meal, unknown fields and rule checks. Saves nothing to the diary;
        returns a preview_id the application can ask the user to confirm."""
        payload = [item.model_dump(exclude_none=True) for item in items]
        return MealPreview(**_run(tools.calculate_meal, ctx, payload, date))

    @server.tool(annotations=WRITE)
    def log_meal(
        preview_id: Annotated[str, Field(pattern=r"^pv_[0-9a-f]{16}$")],
        confirmation_token: Annotated[str, Field(max_length=128, description="Issued by the application, never by you.")],
        request_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{8,80}$",
                                         description="Unique per user request; repeating it never duplicates a meal.")],
    ) -> LogResult:
        """Save a confirmed meal preview to the diary, exactly once."""
        return LogResult(**_run(tools.log_meal, ctx, preview_id, confirmation_token, request_id))

    @server.tool(annotations=WRITE)
    def update_preferences(
        change: PreferenceChange,
        confirmation_token: Annotated[str, Field(max_length=128, description="Issued by the application, never by you.")],
    ) -> PreferenceResult:
        """Save one change the user explicitly confirmed (a preference, restriction or target)."""
        return PreferenceResult(**_run(tools.update_preferences, ctx, change.model_dump(), confirmation_token))

    return server


def context_from_environment() -> tools.ToolContext:
    raw = os.getenv(PROFILE_ENV)
    if not raw or not raw.isdigit():
        raise SystemExit(f"{PROFILE_ENV} must be set to the signed-in profile id")
    return tools.ToolContext(
        db_path=Settings.from_environment().db_path,
        profile_id=int(raw),
        confirmations=Confirmations.from_environment(),
        index_dir=Settings.from_environment().index_dir,
    )


def _warm_up(ctx: tools.ToolContext) -> None:
    """Load the published index and the embedding model while the server waits for its first call."""
    try:
        from .embeddings import default_embedder
        from .index import live
        if ctx.index_dir is not None and live(ctx.index_dir).current() is not None:
            default_embedder().encode(["warm up"])
    except Exception as exc:  # noqa: BLE001 - the search tools report the problem themselves
        print(f"gepeto: retrieval warm-up failed: {exc}", file=sys.stderr)


def main() -> None:
    ctx = context_from_environment()
    threading.Thread(target=_warm_up, args=(ctx,), daemon=True).start()
    build_server(ctx).run()  # stdio


if __name__ == "__main__":
    main()
