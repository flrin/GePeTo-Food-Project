import pytest

from gepeto.profiles import ProfileError, save_profile
from gepeto.targets import MissingInputs, estimate_targets, resting_energy

ANA = {"sex": "female", "age": 28, "height_cm": 165, "weight_kg": 60, "activity_factor": 1.55, "goal": "maintain"}
MIHAI = {"sex": "male", "age": 35, "height_cm": 180, "weight_kg": 85, "activity_factor": 1.375, "goal": "lose"}


def test_mifflin_st_jeor_by_hand() -> None:
    # 10*60 + 6.25*165 - 5*28 - 161 = 1330.25 ; 10*85 + 6.25*180 - 5*35 + 5 = 1805
    assert resting_energy("female", 28, 165, 60) == 1330.25
    assert resting_energy("male", 35, 180, 85) == 1805


def test_maintain_targets_by_hand() -> None:
    t = estimate_targets(ANA)
    # 1330.25 * 1.55 = 2061.89 kcal; protein 1.6*60 = 96 g; fat 30% / 9 = 68.7 g;
    # carbs (2061.89 - 96*4 - 68.73*9) / 4 = 264.8 g
    assert (t.kcal_target, t.protein_target_g, t.fat_target_g, t.carbs_target_g) == (2062, 96, 69, 265)
    assert t.maintenance_kcal == 2061.9 and t.estimate is True


def test_lose_goal_lowers_kcal_and_raises_protein() -> None:
    t = estimate_targets(MIHAI)
    # 1805 * 1.375 * 0.85 = 2109.6 kcal; protein 1.8*85 = 153 g; fat 70.3 g; carbs 216.2 g
    assert (t.kcal_target, t.protein_target_g, t.fat_target_g, t.carbs_target_g) == (2110, 153, 70, 216)


def test_macros_add_up_to_the_kcal_target() -> None:
    for profile in (ANA, MIHAI, {**ANA, "goal": "gain", "activity_factor": 1.9}):
        t = estimate_targets(profile)
        assert abs(t.protein_target_g * 4 + t.carbs_target_g * 4 + t.fat_target_g * 9 - t.kcal_target) <= 8


def test_estimate_is_deterministic() -> None:
    assert estimate_targets(ANA) == estimate_targets(dict(ANA))


def test_missing_inputs_are_listed() -> None:
    with pytest.raises(MissingInputs) as error:
        estimate_targets({"sex": "female", "age": 28})
    assert error.value.missing == ["height_cm", "weight_kg", "activity_factor", "goal"]


@pytest.mark.parametrize("change", [{"age": 16}, {"activity_factor": 3.0}, {"goal": "bulk"}, {"sex": "x"}])
def test_out_of_range_inputs_are_refused(change) -> None:
    with pytest.raises(ValueError):
        estimate_targets({**ANA, **change})


def test_saved_targets_are_labelled_estimate_or_manual(conn) -> None:
    t = estimate_targets(ANA).as_dict()
    targets = {k: t[k] for k in ("kcal_target", "protein_target_g", "carbs_target_g", "fat_target_g")}
    assert save_profile(conn, {"name": "A", **ANA, **targets})["target_source"] == "estimate"
    assert save_profile(conn, {"name": "B", **ANA, **targets, "kcal_target": 1900})["target_source"] == "manual"
    # Targets typed in without body data are fine (the equation is optional).
    assert save_profile(conn, {"name": "C", "kcal_target": 1800})["target_source"] == "manual"
    assert save_profile(conn, {"name": "D"})["target_source"] is None


@pytest.mark.parametrize("bad", [{"name": ""}, {"name": "x", "kcal_target": 50}, {"name": "x", "age": -3},
                                 {"name": "x", "restrictions": "vegan"}, {"name": "x", "weight_kg": "heavy"}])
def test_profile_form_validation(conn, bad) -> None:
    with pytest.raises(ProfileError):
        save_profile(conn, bad)


def test_profile_lists_are_cleaned(conn) -> None:
    saved = save_profile(conn, {"name": "  Ana  ", "preferences": ["Dislikes  Mushrooms", "dislikes mushrooms", " "]})
    assert saved["name"] == "Ana" and saved["preferences"] == ["dislikes mushrooms"]
