# GePeTo - LLM-powered nutrition assistant

Course project for *Understanding and Developing LLMs* (FMI/UBB 2026-2027).
Design: [`docs/RFC.md`](docs/RFC.md), [`docs/MILESTONE_2.md`](docs/MILESTONE_2.md).

## Week 7 (9-15 Nov): data, schema, importer, golden dataset

Plan item: *Set up the Python app, SQLite schema and importer. Select 300 foods, write 30
recipe records and freeze 60 reviewed evaluation cases. Smoke-test local model loading.*

| Evidence | File |
| --- | --- |
| Import script | [`gepeto/importer.py`](gepeto/importer.py), schema in [`gepeto/schema.sql`](gepeto/schema.sql) |
| Source manifest (300 foods) | [`data/source_manifest.csv`](data/source_manifest.csv), made by [`scripts/select_foods.py`](scripts/select_foods.py) |
| Frozen food snapshot (per 100 g) | [`data/foods_300.jsonl`](data/foods_300.jsonl) |
| 30 recipes | [`data/recipes.json`](data/recipes.json) |
| Small fixtures | [`tests/fixtures/`](tests/fixtures/) (mini USDA CSV, user-entered foods with bad rows) |
| Golden dataset + split | [`evaluation/golden.jsonl`](evaluation/golden.jsonl) (36 dev / 24 test), [`evaluation/profiles.json`](evaluation/profiles.json) |
| Validation tests | [`tests/`](tests/), [`evaluation/check_golden.py`](evaluation/check_golden.py) |
| Model smoke test | [`gepeto/smoke_model.py`](gepeto/smoke_model.py), output in [`evaluation/runs/smoke_model.json`](evaluation/runs/smoke_model.json) |

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # week 7 code uses only the standard library + pytest
```

## Reproduce the data

```bash
python scripts/download_usda.py   # SR Legacy 2018-04 zip (CC0), sha256-checked -> data/raw/ (not committed)
python scripts/select_foods.py    # -> data/source_manifest.csv, data/foods_300.jsonl (deterministic)

python -m gepeto.importer usda data/raw/FoodData_Central_sr_legacy_food_csv_2018-04 --manifest data/source_manifest.csv
python -m gepeto.importer recipes data/recipes.json
python -m gepeto.importer foods tests/fixtures/user_foods.json   # example user-entered foods: 2 ok, 4 rejected
```

The database goes to `gepeto.db` (or `$GEPETO_DB`) and is not committed. Running an import
twice reports `unchanged=300`; nothing is duplicated.

### What the importer does

1. **Validate:** a description and source id are required, plus a `source_note` for user-entered foods. Numbers must be non-negative, units must be `kcal` or `kJ`, and a serving needs its gram weight. Rejected rows are printed with the reason.
2. **Normalize:** every value becomes kcal and grams per 100 g. kJ is divided by 4.184. A per-serving label is scaled by `100 / serving_g`. Missing nutrients stay `NULL`, never `0`.
3. **Upsert:** rows are matched by `(source, source_id)`. If the content hash is the same, the row is skipped. If it changed, the row is updated and `version` goes up by one. Every run is logged in `import_runs`.
4. Search indexes (Chroma + BM25) are week 9.

`prep_state` (`raw` / `cooked` / `unspecified`) is guessed from words in the USDA description, so raw and cooked foods stay separate records. `meal_logs` keep a nutrient snapshot and `food_version`, so a later import cannot change old totals. `request_id` is `UNIQUE`, so the same request cannot log a meal twice.

### How the 300 foods were chosen

[`scripts/select_foods.py`](scripts/select_foods.py) takes the 68 recipe ingredients first. It then fills a fixed quota per USDA category (dairy 35, vegetables 50, fruit 40, ...) with the foods that have the shortest descriptions. Ties are broken by `fdc_id`. It skips baby food, brand names and foods missing any of kcal/protein/carbs/fat. The `reason` column in the manifest says why each food was chosen.

## Golden dataset

60 cases, 10 per category: `food_lookup`, `portion_calculation`, `daily_summary`,
`constrained_suggestion`, `preference_change`, `ambiguous_unsupported`. Each category has
6 dev and 4 held-out (`"split": "test"`) cases. Cases that share a `scenario` (paraphrases)
are always in the same split. Profiles are fictional.

Each case has: `turns`, `profile_id`, `diary`, `expected_behaviour`, `expected_facts`,
`acceptable_recipe_ids`, `relevant_source_ids` (`usda:<fdc_id>` / `recipe:<id>`),
`required_actions`, `forbidden_actions`, `expected_db_changes`, plus `calc` / `constraints`
so the expected answer can be recomputed.

```bash
python evaluation/check_golden.py   # 60 cases, Counter({'dev': 36, 'test': 24}), 0 problems
```

The checker imports the frozen data into an in-memory database and recalculates every number
with `gepeto/nutrition.py` (tolerance: 1 kcal, 0.1 g). It rebuilds every suggestion's list of
acceptable recipes from the stated constraints. It also checks counts, the split, scenario
leaks and that every source id exists. **The split is frozen from now on:** we tune on `dev`
and run `test` once, in week 12.

## Tests

```bash
pytest                                   # 23 tests, model test skipped
GEPETO_RUN_MODEL_TESTS=1 pytest          # + live Ollama test
```

## Model smoke test

```bash
ollama pull qwen2.5:3b
python -m gepeto.smoke_model --out evaluation/runs/smoke_model.json
```

Result (14 Nov 2026): `qwen2.5:3b`, 3.1B parameters, Q4_K_M GGUF. It loaded in about 3.8 s and
returned valid JSON that chose `search_foods` for "200 g of raw chicken breast". This only
checks that loading and JSON output work. It is not an evaluation.

## Known limitations (week 7)

- Picking by "shortest description" sometimes chooses unusual foods (emu, ostrich, acorns). They are valid records, just less common.
- The `prep_state` guess is word-based. For example, "canned" counts as cooked.
- Recipe nutrients are **estimates** summed from raw ingredient weights. Only 4 recipes have a `cooked_weight_g` for logging by cooked grams.
- The golden cases are checked by a script against the frozen data.
