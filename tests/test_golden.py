import copy

from evaluation.check_golden import check, load_cases


def test_golden_dataset_is_valid() -> None:
    assert check() == []


def test_checker_catches_a_wrong_number() -> None:
    cases = load_cases()
    broken = copy.deepcopy(cases)
    case = next(c for c in broken if c["category"] == "portion_calculation")
    case["expected_facts"]["kcal"] += 5
    assert any("recalculated" in error for error in check(broken))


def test_checker_catches_a_wrong_suggestion() -> None:
    broken = copy.deepcopy(load_cases())
    case = next(c for c in broken if c["id"] == "constrained_suggestion_06")
    case["acceptable_recipe_ids"].append("r15")
    assert any("acceptable recipes" in error for error in check(broken))


def test_checker_catches_a_split_leak() -> None:
    broken = copy.deepcopy(load_cases())
    next(c for c in broken if c["id"] == "food_lookup_08")["split"] = "dev"
    errors = check(broken)
    assert any("both splits" in error for error in errors)
