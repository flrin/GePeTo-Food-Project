from pathlib import Path

import pytest

from gepeto.db import connect
from gepeto.importer import guess_prep_state, read_food_json, read_usda_csv, upsert_foods

FIXTURES = Path(__file__).with_name("fixtures")


def test_usda_reader_keeps_missing_values_as_none() -> None:
    records = {r["source_id"]: r for r in read_usda_csv(FIXTURES / "usda_mini")}
    assert records["900001"]["kcal"] == 165 and records["900001"]["protein_g"] == 31
    assert records["900002"]["fat_g"] is None  # missing, not zero
    assert records["900001"]["category"] == "Poultry Products"


def test_usda_reader_can_filter_by_manifest_ids() -> None:
    assert [r["source_id"] for r in read_usda_csv(FIXTURES / "usda_mini", {"900002"})] == ["900002"]


@pytest.mark.parametrize(
    "description, state",
    [
        ("Chicken, broilers or fryers, breast, meat only, cooked, roasted", "cooked"),
        ("Spinach, raw", "raw"),
        ("Pasta, dry, enriched", "raw"),
        ("Fish, tuna, light, canned in water", "cooked"),
        ("Honey", "unspecified"),
    ],
)
def test_prep_state(description: str, state: str) -> None:
    assert guess_prep_state(description) == state


def test_import_is_repeatable_without_duplicates() -> None:
    conn = connect()
    rows = read_usda_csv(FIXTURES / "usda_mini")
    first = upsert_foods(conn, rows)
    second = upsert_foods(conn, rows)
    assert (first.inserted, first.unchanged) == (3, 0)
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 3)
    assert conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM import_runs").fetchone()[0] == 2


def test_changed_record_is_updated_and_version_bumped() -> None:
    conn = connect()
    rows = read_usda_csv(FIXTURES / "usda_mini")
    upsert_foods(conn, rows)
    rows[0]["kcal"] = 170
    report = upsert_foods(conn, rows)
    assert (report.updated, report.unchanged) == (1, 2)
    row = conn.execute("SELECT kcal, version FROM foods WHERE source_id = '900001'").fetchone()
    assert (row["kcal"], row["version"]) == (170, 2)


def test_user_foods_are_normalized_and_bad_rows_reported() -> None:
    conn = connect()
    report = upsert_foods(conn, read_food_json(FIXTURES / "user_foods.json"))
    assert report.inserted == 2
    reasons = {source: " ".join(errors) for source, errors in report.rejected}
    assert len(reasons) == 4
    assert "source_note" in reasons["Mystery snack"]
    assert "serving_g" in reasons["Bad serving"]
    assert "negative" in reasons["Negative fat"]
    assert any("missing description" in text for text in reasons.values())

    bar = conn.execute("SELECT * FROM foods WHERE source_id = 'granola-bar-label'").fetchone()
    assert bar["kcal"] == 450  # 180 kcal per 40 g serving -> per 100 g
    assert bar["user_entered"] == 1 and bar["source"] == "user"
    drink = conn.execute("SELECT kcal FROM foods WHERE source_id = 'protein-drink-label-kj'").fetchone()
    assert drink["kcal"] == pytest.approx(100)  # 418.4 kJ
