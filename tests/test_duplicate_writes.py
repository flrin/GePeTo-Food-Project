"""Duplicate-write and confirmation tests: a meal is saved once, and only when confirmed."""

import json
import threading

import pytest

from gepeto import tools
from gepeto.confirm import Confirmations
from gepeto.db import connect
from gepeto.diary import log_preview
from gepeto.errors import GepetoError
from gepeto.importer import upsert_foods
from gepeto.tools import ToolContext

from conftest import DAY
from test_diary_numbers import FOODS


def meal_rows(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM meal_logs").fetchone()[0]


@pytest.fixture
def preview(ctx, food):
    items = [{"food_id": food("171077"), "grams": 200}, {"recipe_id": "r08", "servings": 1}]
    return tools.calculate_meal(ctx, items, DAY)


def token(ctx, preview_id):
    return ctx.confirmations.meal_token(ctx.profile_id, preview_id)


def test_same_request_twice_is_saved_once(ctx, conn, preview) -> None:
    first = tools.log_meal(ctx, preview["preview_id"], token(ctx, preview["preview_id"]), "req-dup-0001")
    second = tools.log_meal(ctx, preview["preview_id"], token(ctx, preview["preview_id"]), "req-dup-0001")
    assert (first["status"], second["status"]) == ("logged", "already_logged")
    assert first["entry_ids"] == second["entry_ids"] and first["totals"] == second["totals"]
    assert meal_rows(conn) == 2  # two items, one meal
    assert tools.get_daily_summary(ctx, DAY)["totals"] == preview["totals"]


def test_same_preview_with_a_new_request_id_is_not_saved_again(ctx, conn, preview) -> None:
    tools.log_meal(ctx, preview["preview_id"], token(ctx, preview["preview_id"]), "req-dup-0002")
    retry = tools.log_meal(ctx, preview["preview_id"], token(ctx, preview["preview_id"]), "req-dup-0003")
    assert retry["status"] == "already_logged" and retry["request_id"] == "req-dup-0002"
    assert meal_rows(conn) == 2


def test_request_id_reused_for_another_meal_is_a_conflict(ctx, conn, preview, food) -> None:
    tools.log_meal(ctx, preview["preview_id"], token(ctx, preview["preview_id"]), "req-dup-0004")
    other = tools.calculate_meal(ctx, [{"food_id": food("171077"), "grams": 50}], DAY)
    with pytest.raises(GepetoError) as error:
        tools.log_meal(ctx, other["preview_id"], token(ctx, other["preview_id"]), "req-dup-0004")
    assert error.value.code == "conflict"
    assert meal_rows(conn) == 2


def test_eating_the_same_thing_twice_needs_two_previews(ctx, conn, food) -> None:
    items = [{"food_id": food("171077"), "grams": 100}]
    for request_id in ("req-twice-01", "req-twice-02"):
        p = tools.calculate_meal(ctx, items, DAY)
        assert tools.log_meal(ctx, p["preview_id"], token(ctx, p["preview_id"]), request_id)["status"] == "logged"
    assert tools.get_daily_summary(ctx, DAY)["totals"]["kcal"] == 240.0


@pytest.mark.parametrize("same_request_id", [True, False])
def test_concurrent_confirms_save_once(db_path, ctx, preview, same_request_id) -> None:
    """8 threads with their own connections confirm the same preview at the same moment."""
    statuses, errors = [], []
    start = threading.Barrier(8)

    def worker(n: int) -> None:
        conn = connect(db_path)
        try:
            start.wait()
            request_id = "req-race-0001" if same_request_id else f"req-race-{n:04d}"
            statuses.append(log_preview(conn, ctx.profile_id, preview["preview_id"], request_id)["status"])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(statuses) == ["already_logged"] * 7 + ["logged"]
    conn = connect(db_path)
    assert meal_rows(conn) == 2
    assert conn.execute("SELECT COUNT(DISTINCT request_id) FROM meal_logs").fetchone()[0] == 1


@pytest.mark.parametrize("bad_token", ["", "0" * 64, "made-up-by-the-model"])
def test_unconfirmed_write_is_refused(ctx, conn, preview, bad_token) -> None:
    with pytest.raises(GepetoError) as error:
        tools.log_meal(ctx, preview["preview_id"], bad_token, "req-unconf-01")
    assert error.value.code == "unconfirmed"
    assert meal_rows(conn) == 0


def test_token_only_works_for_its_own_preview_and_profile(db_path, ctx, conn, preview, food, make_profile) -> None:
    other_preview = tools.calculate_meal(ctx, [{"food_id": food("171077"), "grams": 1}], DAY)
    with pytest.raises(GepetoError):
        tools.log_meal(ctx, preview["preview_id"], token(ctx, other_preview["preview_id"]), "req-swap-001")
    # Another profile's session can't save Ana's preview, even with a token minted for itself.
    mihai = ToolContext(db_path, make_profile("p_mihai"), ctx.confirmations)
    with pytest.raises(GepetoError) as error:
        tools.log_meal(mihai, preview["preview_id"], token(mihai, preview["preview_id"]), "req-swap-002")
    assert error.value.code == "not_found"
    assert meal_rows(conn) == 0


def test_without_a_secret_every_write_is_refused(db_path, ctx, preview) -> None:
    locked = ToolContext(db_path, ctx.profile_id, Confirmations(None))
    with pytest.raises(GepetoError) as error:
        tools.log_meal(locked, preview["preview_id"], token(ctx, preview["preview_id"]), "req-locked-1")
    assert error.value.code == "unavailable"
    assert tools.get_profile(locked)["name"]  # reads still work


def test_preview_is_stale_after_its_food_data_changed(ctx, conn, preview) -> None:
    upsert_foods(conn, [{**FOODS["171077"], "kcal": 150.0}])
    with pytest.raises(GepetoError) as error:
        tools.log_meal(ctx, preview["preview_id"], token(ctx, preview["preview_id"]), "req-stale-01")
    assert error.value.code == "stale_preview"
    assert meal_rows(conn) == 0


def test_a_failed_write_leaves_no_half_saved_meal(ctx, conn, preview) -> None:
    # Corrupt the second item so its INSERT violates a CHECK constraint after the first succeeded.
    items = json.loads(conn.execute("SELECT items FROM meal_previews WHERE id = ?", (preview["preview_id"],)).fetchone()[0])
    items[1]["servings"] = -1
    conn.execute("UPDATE meal_previews SET items = ? WHERE id = ?", (json.dumps(items), preview["preview_id"]))
    conn.commit()
    with pytest.raises(Exception):
        tools.log_meal(ctx, preview["preview_id"], token(ctx, preview["preview_id"]), "req-atomic-1")
    assert meal_rows(conn) == 0
    assert conn.execute("SELECT logged_request_id FROM meal_previews").fetchone()[0] is None


@pytest.mark.parametrize("request_id", ["short", "has spaces in it", "x" * 81, None])
def test_request_id_format(ctx, preview, request_id) -> None:
    with pytest.raises(GepetoError) as error:
        tools.log_meal(ctx, preview["preview_id"], token(ctx, preview["preview_id"]), request_id)
    assert error.value.code == "invalid_input"


def test_preference_change_needs_confirmation_and_is_idempotent(ctx, conn) -> None:
    change = {"field": "preferences", "op": "add", "value": "Dislikes Mushrooms"}
    with pytest.raises(GepetoError) as error:
        tools.update_preferences(ctx, change, "not-a-token")
    assert error.value.code == "unconfirmed"
    normalized = {"field": "preferences", "op": "add", "value": "dislikes mushrooms"}
    good = ctx.confirmations.preference_token(ctx.profile_id, normalized)
    assert tools.update_preferences(ctx, change, good)["changed"] is True
    assert tools.update_preferences(ctx, change, good)["changed"] is False
    assert tools.get_profile(ctx)["preferences"] == ["dislikes mushrooms"]
    # A token for one change can't approve a different one.
    with pytest.raises(GepetoError):
        tools.update_preferences(ctx, {"field": "preferences", "op": "add", "value": "dislikes feta"}, good)


def test_target_change_is_marked_manual(ctx) -> None:
    change = {"field": "kcal_target", "op": "set", "value": 1900.0}
    result = tools.update_preferences(ctx, change, ctx.confirmations.preference_token(ctx.profile_id, change))
    profile = tools.get_profile(ctx)
    assert result["after"] == 1900.0 and profile["targets"]["kcal_target"] == 1900.0
    assert profile["target_source"] == "manual"


def test_week7_database_is_migrated_without_losing_meals(tmp_path) -> None:
    import sqlite3
    path = tmp_path / "week7.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE profiles (id INTEGER PRIMARY KEY, name TEXT NOT NULL, sex TEXT, age INTEGER, height_cm REAL,
            weight_kg REAL, activity_factor REAL, goal TEXT, kcal_target REAL, protein_target_g REAL,
            carbs_target_g REAL, fat_target_g REAL, preferences TEXT NOT NULL DEFAULT '[]',
            restrictions TEXT NOT NULL DEFAULT '[]');
        CREATE TABLE meal_logs (id INTEGER PRIMARY KEY, profile_id INTEGER NOT NULL, log_date TEXT NOT NULL,
            request_id TEXT NOT NULL UNIQUE, food_id INTEGER, food_version INTEGER, recipe_id TEXT, grams REAL,
            servings REAL, kcal REAL, protein_g REAL, carbs_g REAL, fat_g REAL, created_at TEXT NOT NULL);
        CREATE TABLE foods (id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
            description TEXT NOT NULL, category TEXT, prep_state TEXT NOT NULL, kcal REAL, protein_g REAL,
            carbs_g REAL, fat_g REAL, source_version TEXT NOT NULL, import_date TEXT NOT NULL,
            content_hash TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, user_entered INTEGER NOT NULL DEFAULT 0,
            source_note TEXT, UNIQUE (source, source_id));
        INSERT INTO foods (id, source, source_id, description, prep_state, kcal, source_version, import_date, content_hash)
            VALUES (1, 'usda_sr_legacy', '1', 'x', 'raw', 200, 'v', 'then', 'h');
        INSERT INTO profiles (id, name) VALUES (1, 'old');
        INSERT INTO meal_logs (profile_id, log_date, request_id, food_id, grams, kcal, created_at)
            VALUES (1, '2026-11-10', 'req-week7', 1, 200, 400, 'then');
    """)
    old.commit()
    old.close()
    conn = connect(path)
    assert {"item_index", "preview_id", "label"} <= {r["name"] for r in conn.execute("PRAGMA table_info(meal_logs)")}
    assert conn.execute("SELECT request_id, kcal FROM meal_logs").fetchone()[:] == ("req-week7", 400)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
