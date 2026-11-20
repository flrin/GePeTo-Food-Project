"""Confirmation tokens: how the application (not the model) approves a write.

The host (FastAPI) and the MCP server share a random secret that is passed to
the server process through its environment. When the *user* confirms a preview
in the UI, the host signs (profile, preview) with that secret and passes the
token to `log_meal`. The model never sees the secret, so it can't produce a
valid token and can't approve its own writes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets

from .errors import GepetoError

SECRET_ENV = "GEPETO_CONFIRM_SECRET"


def new_secret() -> str:
    return secrets.token_hex(32)


class Confirmations:
    def __init__(self, secret: str | None):
        self._key = secret.encode("utf-8") if secret else None

    @classmethod
    def from_environment(cls) -> "Confirmations":
        return cls(os.getenv(SECRET_ENV))

    def _sign(self, message: str) -> str:
        if self._key is None:
            raise GepetoError("unavailable", "no confirmation secret configured; writes are disabled")
        return hmac.new(self._key, message.encode("utf-8"), hashlib.sha256).hexdigest()

    def meal_token(self, profile_id: int, preview_id: str) -> str:
        return self._sign(f"meal:{profile_id}:{preview_id}")

    def preference_token(self, profile_id: int, change: dict) -> str:
        canonical = json.dumps(change, sort_keys=True, separators=(",", ":"))
        return self._sign(f"pref:{profile_id}:{canonical}")

    def check_meal(self, profile_id: int, preview_id: str, token: str) -> None:
        self._check(self.meal_token(profile_id, preview_id), token)

    def check_preference(self, profile_id: int, change: dict, token: str) -> None:
        self._check(self.preference_token(profile_id, change), token)

    @staticmethod
    def _check(expected: str, token: str) -> None:
        if not isinstance(token, str) or not hmac.compare_digest(expected, token):
            raise GepetoError("unconfirmed", "this write was not confirmed by the user in the application")
