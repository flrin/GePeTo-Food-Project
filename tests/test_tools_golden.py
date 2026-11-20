import anyio

from evaluation.check_golden import load_cases
from evaluation.check_tools import run


def test_golden_dev_cases_through_the_mcp_tools() -> None:
    report = anyio.run(run, "dev")
    summary = report["summary"]
    assert report["failures"] == []
    assert summary["numeric_checks"] >= 30 and summary["suggestion_checks"] >= 8 and summary["db_change_checks"] >= 4
    # Answering never writes: log_meal only ran to seed each case's starting diary.
    diaries = {case["id"]: case["diary"] for case in load_cases()}
    for result in report["results"]:
        assert result["tool_calls"].get("log_meal", 0) == len(diaries[result["id"]])
