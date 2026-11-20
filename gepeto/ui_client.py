"""Small HTTP client the Streamlit UI uses to call the FastAPI app."""

from __future__ import annotations

import os

import httpx

DEFAULT_API_URL = "http://127.0.0.1:8000"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra or {}


def make_http_client() -> httpx.Client:
    """Replaced in tests by a FastAPI TestClient (which is an httpx.Client)."""
    return httpx.Client(base_url=os.getenv("GEPETO_API_URL", DEFAULT_API_URL), timeout=30)


class Api:
    def __init__(self, http: httpx.Client | None = None):
        self.http = http or make_http_client()

    def _send(self, method: str, path: str, **kwargs):
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError(0, "unavailable", f"API not reachable: {exc}") from exc
        if response.status_code >= 400:
            body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            error = body.get("error") if isinstance(body.get("error"), dict) else None
            if error:
                raise ApiError(response.status_code, error.get("code", "error"), error.get("message", ""), error)
            # FastAPI's own validation errors: {"detail": [...]}
            detail = body.get("detail")
            if isinstance(detail, list):
                message = "; ".join(f"{'.'.join(map(str, d.get('loc', [])[1:]))}: {d.get('msg')}" for d in detail)
            else:
                message = str(detail or response.text)
            raise ApiError(response.status_code, "invalid_input", message)
        return response.json()

    def health(self) -> dict:
        return self._send("GET", "/api/health")

    def estimate(self, body: dict) -> dict:
        return self._send("POST", "/api/targets/estimate", json=body)

    def profiles(self) -> list[dict]:
        return self._send("GET", "/api/profiles")

    def save_profile(self, body: dict, profile_id: int | None = None) -> dict:
        if profile_id is None:
            return self._send("POST", "/api/profiles", json=body)
        return self._send("PUT", f"/api/profiles/{profile_id}", json=body)

    def search_foods(self, profile_id: int, query: str, limit: int = 10) -> dict:
        return self._send("GET", f"/api/profiles/{profile_id}/foods", params={"q": query, "limit": limit})

    def recipes(self) -> list[dict]:
        return self._send("GET", "/api/recipes")

    def preview(self, profile_id: int, items: list[dict], day: str) -> dict:
        return self._send("POST", f"/api/profiles/{profile_id}/meals/preview", json={"items": items, "date": day})

    def confirm(self, profile_id: int, preview_id: str, request_id: str) -> dict:
        return self._send("POST", f"/api/profiles/{profile_id}/meals/confirm",
                          json={"preview_id": preview_id, "request_id": request_id})

    def day(self, profile_id: int, day: str) -> dict:
        return self._send("GET", f"/api/profiles/{profile_id}/diary/{day}")

    def delete_meal(self, profile_id: int, request_id: str) -> dict:
        return self._send("DELETE", f"/api/profiles/{profile_id}/meals/{request_id}")
