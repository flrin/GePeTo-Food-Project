from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
EVALUATION_DIR = PROJECT_ROOT / "evaluation"

USDA_RELEASE = "FoodData_Central_sr_legacy_food_csv_2018-04"
USDA_SOURCE = "usda_sr_legacy"
USDA_SOURCE_VERSION = "SR Legacy 2018-04"


@dataclass(frozen=True)
class Settings:
    db_path: Path = PROJECT_ROOT / "gepeto.db"
    ollama_url: str = "http://localhost:11434"
    model_name: str = "qwen2.5:3b"

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            db_path=Path(os.getenv("GEPETO_DB", str(cls.db_path))),
            ollama_url=os.getenv("GEPETO_OLLAMA_URL", cls.ollama_url).rstrip("/"),
            model_name=os.getenv("GEPETO_MODEL", cls.model_name),
        )
