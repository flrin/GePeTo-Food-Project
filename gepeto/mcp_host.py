"""Host side of MCP: FastAPI talks to the tool server through the official client.

One server subprocess per profile (stdio). Its profile id and the confirmation
secret go into the subprocess environment, which is how "the server gets the
user from the trusted session" works in this local prototype.

The SDK's Client must be entered and exited in the same task, so each connection
is owned by a small background task that keeps it open until the host closes.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from .config import PROJECT_ROOT, Settings
from .confirm import SECRET_ENV
from .errors import CODES, GepetoError
from .mcp_server import PROFILE_ENV

CALL_TIMEOUT_S = 30  # the first search in a new server may still be loading the embedding model
PASS_THROUGH_ENV = ("HF_HOME", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_CACHE")
ERROR_PATTERN = re.compile(r"\[(\w+)\] (.*)", re.DOTALL)


def stdio_target(db_path: Path | str, secret: str,
                 index_dir: Path | str | None = None) -> Callable[[int], StdioServerParameters]:
    index_dir = index_dir or Settings.from_environment().index_dir
    extra = {key: os.environ[key] for key in PASS_THROUGH_ENV if key in os.environ}

    def target(profile_id: int) -> StdioServerParameters:
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "gepeto.mcp_server"],
            env={**extra, "GEPETO_DB": str(db_path), "GEPETO_INDEX_DIR": str(index_dir),
                 PROFILE_ENV: str(profile_id), SECRET_ENV: secret},
            cwd=str(PROJECT_ROOT),
        )
    return target


def parse_tool_error(text: str) -> GepetoError:
    """'Error executing tool log_meal: [unconfirmed] ...' -> GepetoError('unconfirmed', ...)."""
    match = ERROR_PATTERN.search(text)
    if match and match.group(1) in CODES:
        return GepetoError(match.group(1), match.group(2).strip())
    # Argument validation failures from the SDK have no code of ours.
    detail = text.split(": ", 1)[-1].strip()
    return GepetoError("invalid_input", detail)


class ToolConnection:
    def __init__(self, target: Any):
        self._target = target
        self._client: Client | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._error: BaseException | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> "ToolConnection":
        self._task = asyncio.create_task(self._own())
        await self._ready.wait()
        if self._error is not None:
            raise GepetoError("unavailable", f"tool server failed to start: {self._error}")
        return self

    async def _own(self) -> None:
        try:
            async with Client(self._target) as client:
                self._client = client
                self._ready.set()
                await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001 - reported to whoever is waiting
            self._error = exc
        finally:
            self._client = None
            self._ready.set()

    @property
    def alive(self) -> bool:
        return self._client is not None and self._task is not None and not self._task.done()

    async def list_tools(self) -> list[dict]:
        if not self.alive:
            raise GepetoError("unavailable", "tool server is not running")
        result = await self._client.list_tools()
        return [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in result.tools]

    async def call(self, name: str, arguments: dict) -> dict:
        if not self.alive:
            raise GepetoError("unavailable", "tool server is not running")
        try:
            with anyio.fail_after(CALL_TIMEOUT_S):
                result = await self._client.call_tool(name, arguments)
        except TimeoutError as exc:
            raise GepetoError("unavailable", f"{name} timed out") from exc
        except GepetoError:
            raise
        except Exception as exc:
            raise GepetoError("unavailable", f"{name} failed: {exc}") from exc
        if result.is_error:
            text = " ".join(getattr(part, "text", "") for part in result.content)
            raise parse_tool_error(text)
        return result.structured_content or {}

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)


class ToolHost:
    """Keeps one tool connection per profile and restarts it if it died."""

    def __init__(self, target_for_profile: Callable[[int], Any]):
        self._target_for_profile = target_for_profile
        self._connections: dict[int, ToolConnection] = {}
        self._lock = asyncio.Lock()

    async def connection(self, profile_id: int) -> ToolConnection:
        async with self._lock:
            conn = self._connections.get(profile_id)
            if conn is None or not conn.alive:
                if conn is not None:
                    await conn.close()
                conn = await ToolConnection(self._target_for_profile(profile_id)).start()
                self._connections[profile_id] = conn
            return conn

    async def call(self, profile_id: int, name: str, arguments: dict) -> dict:
        conn = await self.connection(profile_id)
        return await conn.call(name, arguments)

    async def aclose(self) -> None:
        async with self._lock:
            for conn in self._connections.values():
                await conn.close()
            self._connections.clear()
