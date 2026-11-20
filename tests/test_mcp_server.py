"""The MCP server: tool list, schemas, error codes, profile binding, and a real stdio session."""

import json
import subprocess
import sys

import anyio
import pytest
from mcp import Client

from gepeto.config import PROJECT_ROOT
from gepeto.mcp_host import ToolConnection, parse_tool_error, stdio_target
from gepeto.mcp_server import build_server
from gepeto.tools import READ_ONLY_TOOLS, TOOL_NAMES

from conftest import DAY, SECRET

sys.path.insert(0, str(PROJECT_ROOT))
from scripts.export_tool_schemas import OUT, export, render  # noqa: E402


async def call(server, name, arguments):
    async with Client(server) as client:
        return await client.call_tool(name, arguments)


def run(coro_fn, *args):
    return anyio.run(coro_fn, *args)


def test_the_seven_core_tools_are_exposed(ctx) -> None:
    async def listed():
        async with Client(build_server(ctx)) as client:
            return (await client.list_tools()).tools
    tools = run(listed)
    assert [t.name for t in tools] == list(TOOL_NAMES)
    for tool in tools:
        assert tool.description and tool.input_schema["type"] == "object" and tool.output_schema
        assert tool.annotations.read_only_hint == (tool.name in READ_ONLY_TOOLS)


def test_no_tool_accepts_a_user_or_profile_id() -> None:
    schemas = json.loads(OUT.read_text())
    text = json.dumps([t["inputSchema"] for t in schemas["tools"]])
    assert "profile_id" not in text and "user_id" not in text


def test_committed_schema_file_is_up_to_date() -> None:
    assert OUT.read_text(encoding="utf-8") == render(run(export)), "run scripts/export_tool_schemas.py"


def test_structured_results_and_error_codes(ctx, food) -> None:
    server = build_server(ctx)
    ok = run(call, server, "calculate_meal", {"items": [{"food_id": food("171077"), "grams": 200}], "date": DAY})
    assert not ok.is_error and ok.structured_content["totals"]["kcal"] == 240.0

    cases = [
        ("calculate_meal", {"items": [{"food_id": food("171077"), "grams": -1}]}, "invalid_input"),   # schema
        ("calculate_meal", {"items": [{"food_id": 999999, "grams": 10}]}, "not_found"),
        ("calculate_meal", {"items": [{"recipe_id": "r01", "grams": 10}]}, "missing_data"),
        ("get_daily_summary", {"date": "2026-02-30"}, "invalid_input"),
        ("log_meal", {"preview_id": "pv_0123456789abcdef", "confirmation_token": "x",
                      "request_id": "req-mcp-0001"}, "unconfirmed"),
        ("search_foods", {"query": "??"}, "invalid_input"),
    ]
    for name, arguments, code in cases:
        result = run(call, server, name, arguments)
        assert result.is_error, name
        assert parse_tool_error(result.content[0].text).code == code, (name, result.content[0].text)


def test_profile_comes_from_the_server_not_the_arguments(db_path, make_profile, confirmations, food) -> None:
    from gepeto.tools import ToolContext
    ana, mihai = make_profile("p_ana"), make_profile("p_mihai")
    server = build_server(ToolContext(db_path, ana, confirmations))
    result = run(call, server, "get_profile", {"profile_id": mihai})  # extra argument is ignored
    assert result.structured_content["name"] == "Ana (fictional)"
    assert "age" not in result.structured_content and "weight_kg" not in result.structured_content


def test_search_tools(ctx) -> None:
    server = build_server(ctx)
    foods = run(call, server, "search_foods", {"query": "raw chicken breast", "limit": 3}).structured_content
    assert foods["results"][0]["source_id"] == "usda:171077"
    recipes = run(call, server, "search_recipes", {"meal_type": "dinner", "diet": "vegan", "max_kcal": 500}).structured_content
    assert recipes["results"] and all("vegan" in r["tags"] and r["per_serving"]["kcal"] <= 500 for r in recipes["results"])


def test_stdio_server_round_trip(db_path, make_profile, food) -> None:
    """A real subprocess, started the way the FastAPI host starts it."""
    profile_id = make_profile("p_ana")

    async def session():
        from gepeto.confirm import Confirmations
        conn = await ToolConnection(stdio_target(db_path, SECRET)(profile_id)).start()
        try:
            preview = await conn.call("calculate_meal", {"items": [{"food_id": food("171077"), "grams": 200}], "date": DAY})
            token = Confirmations(SECRET).meal_token(profile_id, preview["preview_id"])
            args = {"preview_id": preview["preview_id"], "confirmation_token": token, "request_id": "req-stdio-01"}
            first = await conn.call("log_meal", args)
            second = await conn.call("log_meal", args)  # e.g. the client retried after a timeout
            summary = await conn.call("get_daily_summary", {"date": DAY})
            return first, second, summary
        finally:
            await conn.close()

    import asyncio
    first, second, summary = asyncio.run(session())
    assert (first["status"], second["status"]) == ("logged", "already_logged")
    assert summary["entry_count"] == 1 and summary["totals"]["kcal"] == 240.0


def test_server_refuses_to_start_without_a_profile() -> None:
    result = subprocess.run([sys.executable, "-m", "gepeto.mcp_server"], capture_output=True, text=True,
                            env={"PATH": "", "GEPETO_DB": ":memory:"}, cwd=PROJECT_ROOT, timeout=60)
    assert result.returncode != 0 and "GEPETO_PROFILE_ID" in result.stderr


def test_unreachable_database_is_reported_as_unavailable(tmp_path, confirmations) -> None:
    from gepeto.tools import ToolContext
    server = build_server(ToolContext(tmp_path / "missing" / "dir" / "x.db", 1, confirmations))
    result = run(call, server, "get_profile", {})
    assert result.is_error and parse_tool_error(result.content[0].text).code == "unavailable"


@pytest.mark.parametrize("text, code", [
    ("Error executing tool log_meal: [conflict] request_id x was already used", "conflict"),
    ("Error executing tool calculate_meal: 1 validation error for calculate_mealArguments", "invalid_input"),
])
def test_host_parses_error_codes(text, code) -> None:
    assert parse_tool_error(text).code == code
