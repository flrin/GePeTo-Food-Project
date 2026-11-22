import sqlite3

import pytest

from gepeto.db import connect, table_names


def add_food(conn, source_id="1", kcal=100.0):
    conn.execute(
        """INSERT INTO foods (source, source_id, description, prep_state, kcal,
               source_version, import_date, content_hash)
           VALUES ('usda_sr_legacy', ?, 'x', 'raw', ?, 'v', 'today', 'h')""",
        (source_id, kcal),
    )


def test_all_tables_are_created() -> None:
    assert table_names(connect()) == [
        "foods", "import_runs", "meal_logs", "meal_previews", "profiles",
        "recipe_ingredients", "recipes",
    ]


def test_same_source_id_cannot_be_inserted_twice() -> None:
    conn = connect()
    add_food(conn)
    with pytest.raises(sqlite3.IntegrityError):
        add_food(conn)


def test_negative_nutrients_are_rejected_by_the_database() -> None:
    with pytest.raises(sqlite3.IntegrityError):
        add_food(connect(), kcal=-5)


def test_repeated_request_id_cannot_duplicate_a_meal() -> None:
    conn = connect()
    add_food(conn)
    conn.execute("INSERT INTO profiles (id, name) VALUES (1, 'test')")
    log = """INSERT INTO meal_logs (profile_id, log_date, request_id, food_id, grams, created_at)
             VALUES (1, '2026-11-10', 'req-1', 1, 100, 'now')"""
    conn.execute(log)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(log)


def test_meal_log_is_either_food_or_recipe() -> None:
    conn = connect()
    conn.execute("INSERT INTO profiles (id, name) VALUES (1, 'test')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO meal_logs (profile_id, log_date, request_id, created_at)
               VALUES (1, '2026-11-10', 'req-2', 'now')"""
        )
