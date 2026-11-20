"""Error codes shared by the services, the MCP tools and the HTTP API.

Milestone 2 asks tool errors to tell apart missing data, invalid quantities and
unavailable services, so every failure carries one of these codes.
"""

from __future__ import annotations

CODES = {
    "invalid_input": 400,    # bad quantity, malformed date, unknown field...
    "not_found": 404,        # unknown food, recipe, preview or profile
    "missing_data": 422,     # the record exists but lacks what we need (e.g. no cooked weight)
    "unconfirmed": 403,      # a write without a valid application confirmation token
    "conflict": 409,         # request id reused for a different meal
    "stale_preview": 409,    # the food data changed after the preview was shown
    "unavailable": 503,      # the tool server / database could not be reached
}


class GepetoError(Exception):
    def __init__(self, code: str, message: str):
        if code not in CODES:
            raise ValueError(f"unknown error code {code}")
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message}

    def __str__(self) -> str:
        # The MCP SDK puts str(exc) into the tool result; the host parses the code back out.
        return f"[{self.code}] {self.message}"
