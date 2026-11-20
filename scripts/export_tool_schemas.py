"""Write the MCP tool list (names, descriptions, input/output JSON schemas) to docs/tool_schemas.json.

The schemas are read from a running server through the MCP client, so the file
shows exactly what a client (and later the model) receives.

    python scripts/export_tool_schemas.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
from mcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gepeto.confirm import Confirmations
from gepeto.config import PROJECT_ROOT
from gepeto.mcp_server import build_server
from gepeto.tools import ToolContext

OUT = PROJECT_ROOT / "docs" / "tool_schemas.json"


async def export() -> dict:
    server = build_server(ToolContext(":memory:", 0, Confirmations(None)))
    async with Client(server) as client:
        listed = await client.list_tools()
    tools = [
        tool.model_dump(mode="json", by_alias=True, exclude_none=True, exclude={"meta"})
        for tool in listed.tools
    ]
    return {"server": "gepeto", "transport": "stdio", "tools": tools}


def render(data: dict) -> str:
    return json.dumps(data, indent=2, sort_keys=False) + "\n"


def main() -> None:
    OUT.write_text(render(anyio.run(export)), encoding="utf-8")
    print(f"wrote {OUT.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
