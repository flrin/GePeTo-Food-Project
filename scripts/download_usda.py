"""Download the frozen USDA FoodData Central SR Legacy release (April 2018, CC0).

    python scripts/download_usda.py        -> data/raw/FoodData_Central_sr_legacy_food_csv_2018-04/
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gepeto.config import DATA_DIR, USDA_RELEASE

URL = f"https://fdc.nal.usda.gov/fdc-datasets/{USDA_RELEASE}.zip"
SHA256 = "b80817294b8850530aaedf2e515c02593b1824f763a0ff356e5c2081643e6fd0"


def main() -> None:
    raw_dir = DATA_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    zip_path = raw_dir / f"{USDA_RELEASE}.zip"
    if not zip_path.exists():
        print(f"downloading {URL}")
        urllib.request.urlretrieve(URL, zip_path)
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    if digest != SHA256:
        raise SystemExit(f"checksum mismatch: {digest} (expected {SHA256})")
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(raw_dir)
    print(f"ok, sha256 verified -> {raw_dir / USDA_RELEASE}")


if __name__ == "__main__":
    main()
