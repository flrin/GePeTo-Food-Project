"""FastAPI application: profile setup, the diary form endpoints and the MCP host.

    uvicorn gepeto.api:app --port 8000

Profile setup and target editing are plain endpoints, because only the user
fills that form. Everything the assistant will also do in week 10 (search,
calculate, log, summarize) goes through the MCP tool server, so the form
already exercises the same tools, schemas and duplicate-write protection.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import diary, profiles, search
from .config import Settings
from .confirm import SECRET_ENV, Confirmations, new_secret
from .db import connect
from .errors import CODES, GepetoError
from .mcp_host import ToolHost, stdio_target
from .mcp_server import MealItem, PreferenceChange
from .targets import MissingInputs, estimate_targets


class ProfileForm(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    sex: Literal["female", "male"] | None = None
    age: int | None = None
    height_cm: float | None = None
    weight_kg: float | None = None
    activity_factor: float | None = None
    goal: Literal["lose", "maintain", "gain"] | None = None
    kcal_target: float | None = None
    protein_target_g: float | None = None
    carbs_target_g: float | None = None
    fat_target_g: float | None = None
    preferences: list[str] = []
    restrictions: list[str] = []


class EstimateForm(BaseModel):
    sex: Literal["female", "male"] | None = None
    age: int | None = None
    height_cm: float | None = None
    weight_kg: float | None = None
    activity_factor: float | None = None
    goal: Literal["lose", "maintain", "gain"] | None = None


class PreviewForm(BaseModel):
    items: list[MealItem] = Field(min_length=1, max_length=20)
    date: str | None = None


class ConfirmForm(BaseModel):
    preview_id: str
    request_id: str = Field(min_length=8, max_length=80)


class PreferenceForm(BaseModel):
    change: PreferenceChange


def create_app(
    db_path: Path | str | None = None,
    secret: str | None = None,
    target_for_profile: Callable[[Confirmations, Path | str], Callable[[int], Any]] | None = None,
    index_dir: Path | str | None = None,
) -> FastAPI:
    """target_for_profile lets tests run the MCP server in-process instead of as a subprocess."""
    db_path = db_path or Settings.from_environment().db_path
    index_dir = index_dir or Settings.from_environment().index_dir
    secret = secret or os.getenv(SECRET_ENV) or new_secret()
    confirmations = Confirmations(secret)
    make_target = target_for_profile or (lambda _c, path: stdio_target(path, secret, index_dir))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        connect(db_path).close()  # create / migrate the schema once, up front
        app.state.tools = ToolHost(make_target(confirmations, db_path))
        yield
        await app.state.tools.aclose()

    app = FastAPI(title="GePeTo", version="0.3.0", lifespan=lifespan)

    @app.exception_handler(GepetoError)
    async def gepeto_error(_: Request, exc: GepetoError):
        return JSONResponse(status_code=CODES[exc.code], content={"error": exc.as_dict()})

    @app.exception_handler(profiles.ProfileError)
    async def profile_error(_: Request, exc: profiles.ProfileError):
        return JSONResponse(status_code=400, content={"error": {"code": "invalid_input", "message": str(exc)}})

    @app.exception_handler(profiles.ProfileNotFound)
    async def profile_missing(_: Request, exc: profiles.ProfileNotFound):
        return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": str(exc)}})

    def db():
        return connect(db_path)

    def require_profile(profile_id: int) -> dict:
        conn = db()
        try:
            return profiles.get_profile(conn, profile_id)
        finally:
            conn.close()

    async def tool(request: Request, profile_id: int, name: str, arguments: dict) -> dict:
        require_profile(profile_id)  # don't start a tool server for a profile that doesn't exist
        return await request.app.state.tools.call(profile_id, name, arguments)

    # ---------------------------------------------------------------- profiles

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "db": str(db_path)}

    @app.post("/api/targets/estimate")
    def estimate(form: EstimateForm):
        try:
            return estimate_targets(form.model_dump()).as_dict()
        except MissingInputs as exc:
            return JSONResponse(status_code=422, content={"error": {
                "code": "missing_data", "message": str(exc), "missing": exc.missing}})
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": {"code": "invalid_input", "message": str(exc)}})

    @app.get("/api/profiles")
    def list_profiles() -> list[dict]:
        conn = db()
        try:
            return profiles.list_profiles(conn)
        finally:
            conn.close()

    @app.post("/api/profiles", status_code=201)
    def create_profile(form: ProfileForm) -> dict:
        conn = db()
        try:
            return profiles.save_profile(conn, form.model_dump())
        finally:
            conn.close()

    @app.get("/api/profiles/{profile_id}")
    def read_profile(profile_id: int) -> dict:
        return require_profile(profile_id)

    @app.put("/api/profiles/{profile_id}")
    def update_profile(profile_id: int, form: ProfileForm) -> dict:
        conn = db()
        try:
            return profiles.save_profile(conn, form.model_dump(), profile_id)
        finally:
            conn.close()

    @app.get("/api/recipes")
    def recipe_catalog() -> list[dict]:
        """All recipes for the logging form. Unlike search_recipes, nothing is filtered out:
        the user may log anything they ate, and the preview's checks flag conflicts."""
        conn = db()
        try:
            return [search.recipe_card(conn, row) for row in conn.execute("SELECT * FROM recipes ORDER BY id")]
        finally:
            conn.close()

    # ---------------------------------------------------------------- diary (through MCP)

    @app.get("/api/profiles/{profile_id}/foods")
    async def foods(request: Request, profile_id: int, q: str, prep_state: str | None = None, limit: int = 10):
        args = {"query": q, "limit": limit, **({"prep_state": prep_state} if prep_state else {})}
        return await tool(request, profile_id, "search_foods", args)

    @app.get("/api/profiles/{profile_id}/recipes")
    async def recipes(request: Request, profile_id: int, q: str | None = None,
                      meal_type: str | None = None, limit: int = 10):
        args = {"limit": limit, **({"query": q} if q else {}), **({"meal_type": meal_type} if meal_type else {})}
        return await tool(request, profile_id, "search_recipes", args)

    @app.post("/api/profiles/{profile_id}/meals/preview")
    async def preview(request: Request, profile_id: int, form: PreviewForm):
        args = {"items": [item.model_dump(exclude_none=True) for item in form.items]}
        if form.date:
            args["date"] = form.date
        return await tool(request, profile_id, "calculate_meal", args)

    @app.post("/api/profiles/{profile_id}/meals/confirm")
    async def confirm(request: Request, profile_id: int, form: ConfirmForm):
        # The user pressed "Save" on this preview: that is the confirmation.
        token = confirmations.meal_token(profile_id, form.preview_id)
        return await tool(request, profile_id, "log_meal", {
            "preview_id": form.preview_id, "confirmation_token": token, "request_id": form.request_id})

    @app.get("/api/profiles/{profile_id}/diary/{day}")
    async def day_summary(request: Request, profile_id: int, day: str):
        return await tool(request, profile_id, "get_daily_summary", {"date": day})

    @app.delete("/api/profiles/{profile_id}/meals/{request_id}")
    def delete_meal(profile_id: int, request_id: str) -> dict:
        require_profile(profile_id)
        conn = db()
        try:
            return {"deleted_entries": diary.delete_meal(conn, profile_id, request_id)}
        finally:
            conn.close()

    @app.post("/api/profiles/{profile_id}/preferences/confirm")
    async def confirm_preference(request: Request, profile_id: int, form: PreferenceForm):
        change = profiles.normalize_change(form.change.model_dump())
        token = confirmations.preference_token(profile_id, change)
        return await tool(request, profile_id, "update_preferences", {
            "change": change, "confirmation_token": token})

    return app


app = create_app()
