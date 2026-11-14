import os

import pytest

from gepeto.smoke_model import run_smoke_test


@pytest.mark.model
@pytest.mark.skipif(os.getenv("GEPETO_RUN_MODEL_TESTS") != "1", reason="needs Ollama")
def test_local_model_returns_a_valid_tool_choice() -> None:
    result = run_smoke_test()
    assert result["valid_json"] and result["tool_is_allowed"]
