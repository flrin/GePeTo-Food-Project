from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 2  # 1 = week 7, 2 = week 8 (multi-item meals, previews, target_source)

_OLD_MEAL_LOG_COLUMNS = (
    "id, profile_id, log_date, request_id, food_id, food_version, recipe_id, "
    "grams, servings, kcal, protein_g, carbs_g, fat_g, created_at"
)


def connect(path: Path | str = ":memory:") -> sqlite3.Connection:
    """Open a database and make sure the schema exists (and is up to date)."""
    conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema)
    _migrate(conn, schema)
    return conn


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection, schema: str) -> None:
    """Bring a week 7 database up to the current schema without losing rows.

    CREATE TABLE IF NOT EXISTS does not change tables that already exist, so the
    few week 8 changes are applied here by hand.
    """
    if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    if "target_source" not in columns(conn, "profiles"):
        conn.execute("ALTER TABLE profiles ADD COLUMN target_source TEXT")
    if "name" not in columns(conn, "recipe_ingredients"):
        conn.execute("ALTER TABLE recipe_ingredients ADD COLUMN name TEXT")
        # Forces the next `importer recipes` run to rewrite recipes (and fill names).
        conn.execute("UPDATE recipes SET content_hash = 'stale'")
    if "item_index" not in columns(conn, "meal_logs"):
        # request_id used to be UNIQUE on its own; a constraint can't be altered in
        # SQLite, so the table is rebuilt and the old rows copied over.
        conn.execute("ALTER TABLE meal_logs RENAME TO meal_logs_week7")
        conn.executescript(schema)
        conn.execute(
            f"INSERT INTO meal_logs ({_OLD_MEAL_LOG_COLUMNS}) "
            f"SELECT {_OLD_MEAL_LOG_COLUMNS} FROM meal_logs_week7"
        )
        conn.execute("DROP TABLE meal_logs_week7")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [row["name"] for row in rows]
