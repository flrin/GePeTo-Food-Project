"""Add the five fictional evaluation profiles (evaluation/profiles.json) to the database.

    python scripts/seed_profiles.py            # skips names that already exist
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gepeto.config import EVALUATION_DIR, Settings
from gepeto.db import connect
from gepeto.profiles import list_profiles, save_profile


def main() -> None:
    db_path = Settings.from_environment().db_path
    conn = connect(db_path)
    existing = {profile["name"] for profile in list_profiles(conn)}
    for profile in json.loads((EVALUATION_DIR / "profiles.json").read_text(encoding="utf-8")):
        if profile["name"] in existing:
            continue
        saved = save_profile(conn, {k: v for k, v in profile.items() if k != "id"})
        print(f"added profile {saved['id']}: {saved['name']}")
    print(f"{len(list_profiles(conn))} profiles in {db_path}")


if __name__ == "__main__":
    main()
