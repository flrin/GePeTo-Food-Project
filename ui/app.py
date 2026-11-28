"""GePeTo - form-based profile setup and food diary (week 8).

    uvicorn gepeto.api:app --port 8000        # API + MCP host
    streamlit run ui/app.py                   # this UI (GEPETO_API_URL, default http://127.0.0.1:8000)

The chat comes in week 10. Everything here goes through the same API and MCP
tools the assistant will use.
"""

from __future__ import annotations

import uuid
from datetime import date

import streamlit as st

from gepeto.targets import ACTIVITY_LEVELS
from gepeto.ui_client import Api, ApiError

NUTRIENTS = [("kcal", "kcal", "kcal_target"), ("protein_g", "Protein (g)", "protein_target_g"),
             ("carbs_g", "Carbs (g)", "carbs_target_g"), ("fat_g", "Fat (g)", "fat_target_g")]
TARGET_KEYS = [target for *_, target in NUTRIENTS]
BODY_KEYS = ["name", "sex", "age", "height_cm", "weight_kg", "activity_factor", "goal"]
ACTIVITY_LABELS = {value: label.replace("_", " ") for label, value in ACTIVITY_LEVELS.items()}
NEW = "+ New profile"

st.set_page_config(page_title="GePeTo diary", page_icon="🥗", layout="wide")


@st.cache_resource
def api() -> Api:
    return Api()


def fmt(value, unit: str = "") -> str:
    if value is None:
        return "unknown"
    return f"{value:,.0f}{unit}" if unit in ("", " kcal") else f"{value:,.1f}{unit}"


def show_error(exc: ApiError) -> None:
    st.error(f"{exc.message}  \n`{exc.code}`")


# ------------------------------------------------------------------ state helpers

def load_profile_into_form(profile: dict | None) -> None:
    profile = profile or {}
    for key in BODY_KEYS + TARGET_KEYS:
        st.session_state[f"pf_{key}"] = profile.get(key)
    activity = st.session_state["pf_activity_factor"]
    if activity is None:
        activity = ACTIVITY_LEVELS["moderate"]
    # The select box only offers the standard factors; snap anything else to the nearest one.
    st.session_state["pf_activity_factor"] = min(ACTIVITY_LABELS, key=lambda v: abs(v - activity))
    st.session_state["pf_name"] = profile.get("name") or ""
    st.session_state["pf_preferences"] = "\n".join(profile.get("preferences", []))
    st.session_state["pf_restrictions"] = "\n".join(profile.get("restrictions", []))
    st.session_state["target_source"] = profile.get("target_source")


def reset_meal() -> None:
    st.session_state["basket"] = []
    st.session_state["preview"] = None
    st.session_state["request_id"] = None


def init_state(profiles: list[dict]) -> None:
    st.session_state.setdefault("basket", [])
    st.session_state.setdefault("preview", None)
    st.session_state.setdefault("request_id", None)
    if "profile_choice" not in st.session_state:
        st.session_state["profile_choice"] = profiles[0]["id"] if profiles else NEW
        load_profile_into_form(profiles[0] if profiles else None)
    saved = st.session_state.pop("pending_profile", None)
    if saved:  # widgets keys can only be changed before the widgets are drawn
        st.session_state["profile_choice"] = saved["id"]
        load_profile_into_form(saved)
    pending = st.session_state.pop("pending_targets", None)
    if pending:  # apply the estimate before the number inputs are created
        for key in TARGET_KEYS:
            st.session_state[f"pf_{key}"] = float(pending[key])


def on_profile_change(profiles_by_id: dict) -> None:
    choice = st.session_state["profile_choice"]
    load_profile_into_form(profiles_by_id.get(choice))
    reset_meal()


# ------------------------------------------------------------------ profile tab

def profile_body() -> dict:
    def lines(key: str) -> list[str]:
        return [line.strip() for line in st.session_state[key].splitlines() if line.strip()]
    body = {key: st.session_state.get(f"pf_{key}") for key in BODY_KEYS + TARGET_KEYS}
    body["preferences"] = lines("pf_preferences")
    body["restrictions"] = lines("pf_restrictions")
    return body


def profile_tab(profile_id) -> None:
    st.subheader("Profile and daily targets")
    with st.form("profile_form"):
        st.text_input("Name", key="pf_name", max_chars=80)
        c1, c2, c3, c4 = st.columns(4)
        c1.selectbox("Sex", [None, "female", "male"], key="pf_sex", format_func=lambda v: v or "-")
        c2.number_input("Age", min_value=1, max_value=120, step=1, key="pf_age")
        c3.number_input("Height (cm)", min_value=50.0, max_value=260.0, step=1.0, key="pf_height_cm")
        c4.number_input("Weight (kg)", min_value=20.0, max_value=400.0, step=0.5, key="pf_weight_kg")
        c1, c2 = st.columns(2)
        c1.selectbox("Activity", list(ACTIVITY_LABELS), key="pf_activity_factor",
                     format_func=lambda v: f"{ACTIVITY_LABELS[v]} (x{v})")
        c2.selectbox("Goal", [None, "lose", "maintain", "gain"], key="pf_goal", format_func=lambda v: v or "-")

        st.markdown("**Daily targets** - estimate them from the fields above, or type your own.")
        cols = st.columns(4)
        for col, (_, label, key) in zip(cols, NUTRIENTS):
            col.number_input(label, min_value=0.0, max_value=10000.0, step=1.0, key=f"pf_{key}")
        source = st.session_state.get("target_source")
        if source == "estimate":
            st.caption("Saved targets are the Mifflin-St Jeor estimate (an estimate, not medical advice).")
        elif source == "manual":
            st.caption("Saved targets were entered or edited by you.")

        c1, c2 = st.columns(2)
        c1.text_area("Preferences (one per line)", key="pf_preferences", placeholder="dislikes mushrooms")
        c2.text_area("Restrictions (one per line)", key="pf_restrictions", placeholder="vegetarian\nno shrimp")
        b1, b2, _ = st.columns([1, 1, 3])
        estimate_clicked = b1.form_submit_button("Estimate targets")
        save_clicked = b2.form_submit_button("Save profile", type="primary")

    if estimate_clicked:
        body = profile_body()
        try:
            estimate = api().estimate({key: body[key] for key in BODY_KEYS if key != "name"})
        except ApiError as exc:
            missing = exc.extra.get("missing")
            if missing:
                st.warning("To estimate, fill in: " + ", ".join(missing) + ". Or type your targets directly.")
            else:
                show_error(exc)
        else:
            st.session_state["pending_targets"] = estimate
            st.session_state["flash"] = (
                f"Estimate: resting {estimate['resting_kcal']:.0f} kcal, maintenance "
                f"{estimate['maintenance_kcal']:.0f} kcal. Review the targets, then save."
            )
            st.rerun()

    if save_clicked:
        try:
            saved = api().save_profile(profile_body(), None if profile_id == NEW else profile_id)
        except ApiError as exc:
            show_error(exc)
        else:
            st.session_state["pending_profile"] = saved
            st.session_state["flash"] = f"Saved profile '{saved['name']}'."
            st.rerun()


# ------------------------------------------------------------------ diary tab

def add_to_basket(item: dict, label: str) -> None:
    st.session_state["basket"].append({"item": item, "label": label})
    st.session_state["preview"] = None  # the meal changed, so the old preview is void
    st.session_state["request_id"] = None


def meal_builder(profile_id: int) -> None:
    food_col, recipe_col = st.columns(2)
    with food_col:
        st.markdown("**Add a food**")
        query = st.text_input("Search foods", key="food_query", placeholder="e.g. raw chicken breast")
        results, retrieval = [], None
        if query.strip():
            try:
                found = api().search_foods(profile_id, query, limit=10)
                results, retrieval = found["results"], found["retrieval"]
            except ApiError as exc:
                show_error(exc)
        if retrieval and retrieval["method"] != "hybrid":
            st.caption(f"Keyword search only: {retrieval['reason']}")
        elif retrieval and retrieval["index_up_to_date"] is False:
            st.caption("The search index is older than the food data. Rebuild it: python -m gepeto.index build")
        if results:
            options = {r["food_id"]: r for r in results}
            chosen = st.selectbox(
                "Food", list(options), key="food_choice",
                format_func=lambda fid: f"{options[fid]['description']} - {fmt(options[fid]['per_100g']['kcal'])} kcal/100 g",
            )
            grams = st.number_input("Grams", min_value=1.0, max_value=5000.0, value=100.0, step=10.0, key="food_grams")
            if st.button("Add food", key="add_food"):
                add_to_basket({"food_id": chosen, "grams": grams}, f"{grams:g} g {options[chosen]['description']}")
                st.rerun()
        elif query.strip():
            st.caption("No food matched. Try fewer or different words.")

    with recipe_col:
        st.markdown("**Add a recipe**")
        recipes = {r["recipe_id"]: r for r in st.session_state["recipes"]}
        chosen = st.selectbox(
            "Recipe", list(recipes), key="recipe_choice",
            format_func=lambda rid: f"{recipes[rid]['title']} ({recipes[rid]['meal_type']}, "
                                    f"~{fmt(recipes[rid]['per_serving']['kcal'])} kcal/serving)",
        )
        recipe = recipes[chosen]
        modes = ["servings"] + (["cooked grams"] if recipe["cooked_weight_g"] else [])
        mode = st.radio("Amount in", modes, horizontal=True, key="recipe_mode")
        if mode == "servings":
            amount = st.number_input("Servings", min_value=0.25, max_value=20.0, value=1.0, step=0.25, key="recipe_servings")
            item, label = {"recipe_id": chosen, "servings": amount}, f"{amount:g} serving(s) {recipe['title']}"
        else:
            amount = st.number_input("Cooked grams", min_value=1.0, max_value=5000.0, value=300.0, step=10.0, key="recipe_grams")
            item, label = {"recipe_id": chosen, "grams": amount}, f"{amount:g} g cooked {recipe['title']}"
        if not recipe["cooked_weight_g"]:
            st.caption("No cooked weight recorded for this recipe, so it can only be logged by servings.")
        if st.button("Add recipe", key="add_recipe"):
            add_to_basket(item, label)
            st.rerun()


def meal_review(profile_id: int, day: str) -> None:
    basket = st.session_state["basket"]
    st.markdown("**This meal**")
    if not basket:
        st.caption("Nothing added yet.")
        return
    for index, entry in enumerate(basket):
        c1, c2 = st.columns([6, 1])
        c1.write(f"{index + 1}. {entry['label']}")
        if c2.button("Remove", key=f"rm_{index}"):
            basket.pop(index)
            st.session_state["preview"] = None
            st.session_state["request_id"] = None
            st.rerun()

    c1, c2, _ = st.columns([1, 1, 3])
    if c1.button("Calculate", type="primary", key="calculate"):
        try:
            preview = api().preview(profile_id, [entry["item"] for entry in basket], day)
        except ApiError as exc:
            show_error(exc)
        else:
            st.session_state["preview"] = preview
            # One request id per preview: pressing Save twice (or a rerun) can't log twice.
            st.session_state["request_id"] = f"form-{uuid.uuid4().hex}"
    if c2.button("Clear meal", key="clear"):
        reset_meal()
        st.rerun()

    preview = st.session_state["preview"]
    if preview is None:
        return
    st.markdown(f"**Preview for {preview['date']}** (nothing is saved yet)")
    rows = [
        {"item": item["label"], "source": item["source_id"],
         "grams": item["grams"], "servings": item["servings"],
         **{label: item["nutrients"][name] for name, label, _ in NUTRIENTS},
         "estimate": "yes" if item["estimate"] else ""}
        for item in preview["items"]
    ]
    rows.append({"item": "Total", **{label: preview["totals"][name] for name, label, _ in NUTRIENTS}})
    st.dataframe(rows, hide_index=True)
    if preview["unknown_fields"]:
        st.warning("Unknown for at least one item (shown as empty, not zero): " + ", ".join(preview["unknown_fields"]))
    for check in preview["checks"]:
        icon = {"pass": "✅", "fail": "⚠️", "unknown": "❔"}[check["status"]]
        st.caption(f"{icon} {check['rule']}: {check['status']} ({check['detail']})")
    for text in preview["unapplied_restrictions"]:
        st.caption(f"❔ '{text}' could not be checked automatically.")
    left = preview["remaining_after"]
    st.caption("Left after this meal: " + ", ".join(
        f"{label} {fmt(left[name])}" for name, label, _ in NUTRIENTS))

    if st.button("Save to diary", type="primary", key="save_meal"):
        try:
            result = api().confirm(profile_id, preview["preview_id"], st.session_state["request_id"])
        except ApiError as exc:
            show_error(exc)
        else:
            reset_meal()
            st.session_state["flash"] = (
                "Saved to the diary." if result["status"] == "logged" else "Already saved - nothing was duplicated."
            )
            st.rerun()


def day_view(profile_id: int, day: str) -> None:
    try:
        summary = api().day(profile_id, day)
    except ApiError as exc:
        show_error(exc)
        return
    st.markdown(f"**Diary for {summary['date']}**")
    cols = st.columns(4)
    for col, (name, label, _) in zip(cols, NUTRIENTS):
        total, target, left = summary["totals"][name], summary["targets"][name], summary["remaining"][name]
        unit = " kcal" if name == "kcal" else " g"
        col.metric(label, fmt(total, unit), None if left is None else f"{fmt(left, unit)} left", delta_color="off")
        if target:
            col.progress(min(max((total or 0) / target, 0.0), 1.0), text=f"target {fmt(target, unit)}")
        else:
            col.caption("no target set")
    if summary["unknown_fields"]:
        st.warning("Some logged items have unknown values for: " + ", ".join(summary["unknown_fields"]))
    if not summary["meals"]:
        st.caption("No meals logged on this day.")
    for meal in summary["meals"]:
        with st.container(border=True):
            c1, c2 = st.columns([6, 1])
            c1.write(" + ".join(item["label"] or item["source_id"] for item in meal["items"]))
            c1.caption(" · ".join(f"{label} {fmt(meal['totals'][name])}" for name, label, _ in NUTRIENTS))
            if c2.button("Delete", key=f"del_{meal['request_id']}"):
                try:
                    api().delete_meal(profile_id, meal["request_id"])
                except ApiError as exc:
                    show_error(exc)
                else:
                    st.rerun()


def diary_tab(profile_id) -> None:
    if profile_id == NEW:
        st.info("Save a profile first.")
        return
    if "recipes" not in st.session_state:
        st.session_state["recipes"] = api().recipes()
    day = st.date_input("Day", value=date.today(), key="diary_day").isoformat()
    meal_builder(profile_id)
    st.divider()
    meal_review(profile_id, day)
    st.divider()
    day_view(profile_id, day)


# ------------------------------------------------------------------ page

def main() -> None:
    st.title("GePeTo food diary")
    try:
        profiles = api().profiles()
    except ApiError as exc:
        st.error(f"Cannot reach the GePeTo API. Start it with `uvicorn gepeto.api:app`.  \n{exc.message}")
        st.stop()
    profiles_by_id = {p["id"]: p for p in profiles}
    init_state(profiles)

    with st.sidebar:
        st.selectbox(
            "Profile", [*profiles_by_id, NEW], key="profile_choice",
            format_func=lambda pid: pid if pid == NEW else profiles_by_id[pid]["name"],
            on_change=on_profile_change, args=(profiles_by_id,),
        )
        st.caption("Fictional profiles only. Data stays in the local SQLite file.")
    flash = st.session_state.pop("flash", None)
    if flash:
        st.success(flash)

    profile_id = st.session_state["profile_choice"]
    diary, profile = st.tabs(["Diary", "Profile & targets"])
    with diary:
        diary_tab(profile_id)
    with profile:
        profile_tab(profile_id)


main()
