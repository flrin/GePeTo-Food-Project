"""Smoke test: can we load the local model through Ollama and get a JSON tool choice?

    ollama pull qwen2.5:3b
    python -m gepeto.smoke_model                       # GEPETO_MODEL to override
    python -m gepeto.smoke_model --out evaluation/runs/smoke_model.json

This is not an evaluation; it only checks that model loading, the HTTP API and
JSON output work before we build the agent controller (week 10).
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings

ALLOWED_TOOLS = [
    "get_profile", "get_daily_summary", "search_foods", "search_recipes",
    "calculate_meal", "log_meal", "update_preferences",
]
SYSTEM_PROMPT = (
    "You are the controller of a nutrition app. Choose exactly one tool for the user's "
    f"request from this list: {', '.join(ALLOWED_TOOLS)}. "
    'Reply only with JSON: {"tool": "<name>", "arguments": {...}}.'
)
QUESTION = "What are the calories and protein in 200 g of raw chicken breast?"


def _post(url: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def run_smoke_test(settings: Settings | None = None, timeout: float = 300) -> dict:
    settings = settings or Settings.from_environment()
    with urllib.request.urlopen(f"{settings.ollama_url}/api/tags", timeout=10) as response:
        available = [model["name"] for model in json.loads(response.read())["models"]]
    if settings.model_name not in available:
        raise SystemExit(f"{settings.model_name} not pulled; available: {available}. Run: ollama pull {settings.model_name}")

    details = _post(f"{settings.ollama_url}/api/show", {"model": settings.model_name}, timeout)["details"]
    started = time.perf_counter()
    reply = _post(
        f"{settings.ollama_url}/api/chat",
        {
            "model": settings.model_name,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": QUESTION},
            ],
            "format": "json",
            "stream": False,
            "options": {"temperature": 0, "seed": 7},
        },
        timeout,
    )
    latency_ms = (time.perf_counter() - started) * 1000
    text = reply["message"]["content"]
    try:
        action = json.loads(text)
    except json.JSONDecodeError:
        action = None
    valid_json = isinstance(action, dict)
    tool = action.get("tool") if isinstance(action, dict) else None
    return {
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": settings.model_name,
        "parameter_size": details.get("parameter_size"),
        "quantization": details.get("quantization_level"),
        "format": details.get("format"),
        "question": QUESTION,
        "raw_reply": text,
        "valid_json": valid_json,
        "tool": tool,
        "tool_is_allowed": tool in ALLOWED_TOOLS,
        "latency_ms": round(latency_ms),
        "load_ms": round(reply.get("load_duration", 0) / 1e6),
        "output_tokens": reply.get("eval_count"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, help="also save the result as JSON")
    args = parser.parse_args()
    result = run_smoke_test()
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    raise SystemExit(0 if result["valid_json"] and result["tool_is_allowed"] else 1)


if __name__ == "__main__":
    main()
