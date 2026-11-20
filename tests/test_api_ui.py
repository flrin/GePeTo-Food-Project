"""The FastAPI app (with the real stdio MCP server behind it) and the Streamlit form flow."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from gepeto.api import create_app
from gepeto.config import PROJECT_ROOT

from conftest import DAY, SECRET

ANA_FORM = {"name": "Ana (fictional)", "sex": "female", "age": 28, "height_cm": 165, "weight_kg": 60,
            "activity_factor": 1.55, "goal": "maintain"}


@pytest.fixture
def api(db_path):
    with TestClient(create_app(db_path=db_path, secret=SECRET)) as client:
        yield client


def test_profile_setup_with_estimated_targets(api) -> None:
    estimate = api.post("/api/targets/estimate", json={k: v for k, v in ANA_FORM.items() if k != "name"}).json()
    assert estimate["kcal_target"] == 2062 and estimate["estimate"] is True
    targets = {k: estimate[k] for k in ("kcal_target", "protein_target_g", "carbs_target_g", "fat_target_g")}
    created = api.post("/api/profiles", json={**ANA_FORM, **targets})
    assert created.status_code == 201 and created.json()["target_source"] == "estimate"
    edited = api.put(f"/api/profiles/{created.json()['id']}", json={**ANA_FORM, **targets, "kcal_target": 1950})
    assert edited.json()["target_source"] == "manual"


def test_missing_estimate_inputs_and_bad_forms(api) -> None:
    response = api.post("/api/targets/estimate", json={"sex": "female"})
    assert response.status_code == 422 and "age" in response.json()["error"]["missing"]
    assert api.post("/api/targets/estimate", json={**ANA_FORM, "age": 15}).status_code == 400
    assert api.post("/api/profiles", json={"name": "x", "kcal_target": 20}).status_code == 400
    assert api.get("/api/profiles/999").status_code == 404
    assert api.get("/api/profiles/999/diary/2026-11-18").status_code == 404


def test_form_logging_flow_through_mcp(api, food) -> None:
    pid = api.post("/api/profiles", json={**ANA_FORM, "kcal_target": 2000}).json()["id"]
    found = api.get(f"/api/profiles/{pid}/foods", params={"q": "raw chicken breast"}).json()
    assert found["results"][0]["source_id"] == "usda:171077"
    assert len(api.get("/api/recipes").json()) == 30

    items = [{"food_id": found["results"][0]["food_id"], "grams": 200}, {"recipe_id": "r08", "grams": 300}]
    preview = api.post(f"/api/profiles/{pid}/meals/preview", json={"items": items, "date": DAY}).json()
    assert preview["logged"] is False and preview["totals"]["kcal"] == pytest.approx(240 + 181.6, abs=1)
    assert api.get(f"/api/profiles/{pid}/diary/{DAY}").json()["entry_count"] == 0

    body = {"preview_id": preview["preview_id"], "request_id": "form-test-0001"}
    results = [api.post(f"/api/profiles/{pid}/meals/confirm", json=body).json() for _ in range(3)]  # triple click
    assert [r["status"] for r in results] == ["logged", "already_logged", "already_logged"]
    day = api.get(f"/api/profiles/{pid}/diary/{DAY}").json()
    assert day["entry_count"] == 2 and day["remaining"]["kcal"] == pytest.approx(2000 - 421.6, abs=1)

    assert api.delete(f"/api/profiles/{pid}/meals/form-test-0001").json() == {"deleted_entries": 2}
    assert api.get(f"/api/profiles/{pid}/diary/{DAY}").json()["entry_count"] == 0


def test_api_error_codes(api) -> None:
    pid = api.post("/api/profiles", json=ANA_FORM).json()["id"]
    bad = api.post(f"/api/profiles/{pid}/meals/preview", json={"items": [{"recipe_id": "r01", "grams": 100}]})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "missing_data"
    missing = api.post(f"/api/profiles/{pid}/meals/confirm", json={"preview_id": "pv_0123456789abcdef",
                                                                  "request_id": "form-test-0002"})
    assert missing.status_code == 404
    assert api.post(f"/api/profiles/{pid}/meals/preview", json={"items": []}).status_code == 422


def test_confirmed_preference_change(api) -> None:
    pid = api.post("/api/profiles", json=ANA_FORM).json()["id"]
    change = {"change": {"field": "restrictions", "op": "add", "value": "vegetarian"}}
    assert api.post(f"/api/profiles/{pid}/preferences/confirm", json=change).json()["changed"] is True
    assert api.post(f"/api/profiles/{pid}/preferences/confirm", json=change).json()["changed"] is False
    assert api.get(f"/api/profiles/{pid}").json()["restrictions"] == ["vegetarian"]


def test_streamlit_form_flow(api, monkeypatch) -> None:
    """Fill the profile form, estimate, save, build a meal, calculate, save twice."""
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    import gepeto.ui_client
    monkeypatch.setattr(gepeto.ui_client, "make_http_client", lambda: api)
    st.cache_resource.clear()

    at = AppTest.from_file(str(PROJECT_ROOT / "ui" / "app.py"), default_timeout=60).run()
    assert not at.exception
    at.text_input(key="pf_name").set_value("Ana (fictional)")
    at.selectbox(key="pf_sex").set_value("female")
    at.number_input(key="pf_age").set_value(28)
    at.number_input(key="pf_height_cm").set_value(165.0)
    at.number_input(key="pf_weight_kg").set_value(60.0)
    at.selectbox(key="pf_goal").set_value("maintain")
    next(b for b in at.button if b.label == "Estimate targets").click().run()
    assert at.number_input(key="pf_kcal_target").value == 2062.0
    next(b for b in at.button if b.label == "Save profile").click().run()
    assert not at.exception and at.session_state["profile_choice"] == 1

    at.text_input(key="food_query").set_value("raw chicken breast").run()
    at.number_input(key="food_grams").set_value(200.0)
    at.button(key="add_food").click().run()
    at.button(key="add_recipe").click().run()  # default: 1 serving of the first recipe
    at.button(key="calculate").click().run()
    preview = at.session_state["preview"]
    assert preview["items"][0]["nutrients"]["kcal"] == 240.0
    request_id = at.session_state["request_id"]
    at.button(key="save_meal").click().run()
    assert not at.exception and "Saved to the diary." in [s.value for s in at.success]

    # The same confirmation arriving again (double click, network retry) changes nothing.
    again = api.post("/api/profiles/1/meals/confirm", json={"preview_id": preview["preview_id"], "request_id": request_id})
    assert again.json()["status"] == "already_logged"
    day = api.get(f"/api/profiles/1/diary/{date.today().isoformat()}").json()
    assert day["entry_count"] == 2 and day["totals"]["kcal"] == preview["totals"]["kcal"]
