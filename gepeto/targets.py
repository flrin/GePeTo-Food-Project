"""Daily target estimate from fixed formulas (Milestone 2, section 3).

    resting energy (Mifflin-St Jeor) = 10 * kg + 6.25 * cm - 5 * age + 5     (male)
                                       10 * kg + 6.25 * cm - 5 * age - 161   (female)
    maintenance = resting energy * activity factor
    kcal target = maintenance * goal factor

    protein = 1.6 g per kg body weight (1.8 g/kg when the goal is to lose weight)
    fat     = 30 % of the kcal target, 9 kcal per gram
    carbs   = whatever kcal are left, 4 kcal per gram

This is a general estimate for adults, not a medical prescription. The user can
always overwrite the numbers, and the language model never calls this.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

ACTIVITY_LEVELS = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "active": 1.725,
    "very_active": 1.9,
}
GOAL_FACTORS = {"lose": 0.85, "maintain": 1.0, "gain": 1.10}
PROTEIN_G_PER_KG = {"lose": 1.8, "maintain": 1.6, "gain": 1.6}
FAT_SHARE = 0.30
KCAL_PER_G = {"protein_g": 4, "carbs_g": 4, "fat_g": 9}
MIN_ACTIVITY, MAX_ACTIVITY = 1.2, 2.5
MIN_AGE, MAX_AGE = 18, 100
REQUIRED = ("sex", "age", "height_cm", "weight_kg", "activity_factor", "goal")


class MissingInputs(ValueError):
    """Some equation input is missing; the user can type targets manually instead."""

    def __init__(self, missing: list[str]):
        super().__init__("cannot estimate targets, missing: " + ", ".join(missing))
        self.missing = missing


@dataclass(frozen=True)
class Targets:
    kcal_target: float
    protein_target_g: float
    carbs_target_g: float
    fat_target_g: float
    resting_kcal: float
    maintenance_kcal: float
    estimate: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


def resting_energy(sex: str, age: float, height_cm: float, weight_kg: float) -> float:
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base + 5 if sex == "male" else base - 161


def estimate_targets(profile: dict) -> Targets:
    """profile needs sex, age, height_cm, weight_kg, activity_factor and goal."""
    missing = [key for key in REQUIRED if profile.get(key) in (None, "")]
    if missing:
        raise MissingInputs(missing)
    sex, goal = profile["sex"], profile["goal"]
    age, height, weight = float(profile["age"]), float(profile["height_cm"]), float(profile["weight_kg"])
    activity = float(profile["activity_factor"])
    if sex not in ("female", "male"):
        raise ValueError("sex must be 'female' or 'male' for this equation")
    if goal not in GOAL_FACTORS:
        raise ValueError(f"goal must be one of {sorted(GOAL_FACTORS)}")
    if not MIN_AGE <= age <= MAX_AGE:
        raise ValueError(f"the equation is meant for adults ({MIN_AGE}-{MAX_AGE}); enter targets manually")
    if not MIN_ACTIVITY <= activity <= MAX_ACTIVITY:
        raise ValueError(f"activity factor must be between {MIN_ACTIVITY} and {MAX_ACTIVITY}")
    if height <= 0 or weight <= 0:
        raise ValueError("height and weight must be positive")

    resting = resting_energy(sex, age, height, weight)
    maintenance = resting * activity
    kcal = maintenance * GOAL_FACTORS[goal]  # >= resting, since 1.2 * 0.85 > 1
    protein = PROTEIN_G_PER_KG[goal] * weight
    fat = kcal * FAT_SHARE / KCAL_PER_G["fat_g"]
    carbs = max(kcal - protein * KCAL_PER_G["protein_g"] - fat * KCAL_PER_G["fat_g"], 0) / KCAL_PER_G["carbs_g"]
    return Targets(
        kcal_target=round(kcal),
        protein_target_g=round(protein),
        carbs_target_g=round(carbs),
        fat_target_g=round(fat),
        resting_kcal=round(resting, 1),
        maintenance_kcal=round(maintenance, 1),
    )
