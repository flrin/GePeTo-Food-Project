# GePeTo - LLM-powered nutrition assistant

This is our course project for *Understanding and Developing LLMs* (FMI/UBB 2026-2027). It is the first time we build something like this, so a lot of the choices below are our best guess after reading and trying things out. If something looks odd, it probably is a beginner decision and we are happy to change it.

The design is explained in [`docs/RFC.md`](docs/RFC.md) (the idea) and [`docs/MILESTONE_2.md`](docs/MILESTONE_2.md) (architecture and plan).

## Week 7 (9-15 Nov): data, schema, importer, golden dataset

What the plan asked for this week: *Set up the Python app, SQLite schema and importer. Select 300 foods, write 30 recipe records and freeze 60 reviewed evaluation cases. Smoke-test local model loading.*

Here is where each part of that lives in the repo:

| What we did | Where it is |
| --- | --- |
| Import script | [`gepeto/importer.py`](gepeto/importer.py), with the database schema in [`gepeto/schema.sql`](gepeto/schema.sql) |
| List of the 300 chosen foods | [`data/source_manifest.csv`](data/source_manifest.csv), made by [`scripts/select_foods.py`](scripts/select_foods.py) |
| Frozen copy of the foods (per 100 g) | [`data/foods_300.jsonl`](data/foods_300.jsonl) |
| 30 recipes | [`data/recipes.json`](data/recipes.json) |
| Small test data | [`tests/fixtures/`](tests/fixtures/) (a tiny USDA CSV and some user-entered foods, including bad rows on purpose) |
| Golden dataset and dev/test split | [`evaluation/golden.jsonl`](evaluation/golden.jsonl) (36 dev / 24 test), [`evaluation/profiles.json`](evaluation/profiles.json) |
| Checks for all of the above | [`tests/`](tests/), [`evaluation/check_golden.py`](evaluation/check_golden.py) |
| Check that the local model loads | [`gepeto/smoke_model.py`](gepeto/smoke_model.py), output in [`evaluation/runs/smoke_model.json`](evaluation/runs/smoke_model.json) |

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # week 7 code uses only the standard library + pytest
```

If you are new to this like us: the first line makes a separate Python environment so nothing gets installed globally, and the second installs our package plus pytest.

## Reproduce the data

```bash
python scripts/download_usda.py   # SR Legacy 2018-04 zip (CC0), sha256-checked -> data/raw/ (not committed)
python scripts/select_foods.py    # -> data/source_manifest.csv, data/foods_300.jsonl (deterministic)

python -m gepeto.importer usda data/raw/FoodData_Central_sr_legacy_food_csv_2018-04 --manifest data/source_manifest.csv
python -m gepeto.importer recipes data/recipes.json
python -m gepeto.importer foods tests/fixtures/user_foods.json   # example user-entered foods: 2 ok, 4 rejected
```

The database is saved to `gepeto.db` (or wherever `$GEPETO_DB` points) and we don't commit it. If you run an import twice you should see `unchanged=300` the second time, which means nothing got duplicated.

### What the importer does

We split the import into three steps, so it is easier to understand and to test:

1. **Validate.** A description and a source id are required, and user-entered foods also need a `source_note`. Numbers can't be negative, the unit has to be `kcal` or `kJ`, and a serving needs its weight in grams. Rows that fail are printed together with the reason.
2. **Normalize.** Everything is converted to kcal and grams per 100 g. kJ is divided by 4.184, and a per-serving label is scaled by `100 / serving_g`. If a nutrient is missing we store `NULL`, not `0`, because "unknown" and "zero" are not the same thing.
3. **Upsert** (insert or update). Rows are matched by `(source, source_id)`. If the content hash is the same the row is skipped, and if it changed the row is updated and `version` goes up by one. Every run is written to the `import_runs` table.

The search indexes (Chroma + BM25) are planned for week 9, so they are not here yet.

A few other things we decided:

- `prep_state` (`raw` / `cooked` / `unspecified`) is guessed from words in the USDA description, so raw and cooked foods stay separate records.
- `meal_logs` keep a snapshot of the nutrients and a `food_version`, so importing new data later can't change totals from earlier days.
- `request_id` is `UNIQUE`, so the same request can't log a meal twice.

### How the 300 foods were chosen

[`scripts/select_foods.py`](scripts/select_foods.py) starts with the 68 ingredients used in our recipes. Then it fills a fixed quota for each USDA category (dairy 35, vegetables 50, fruit 40, ...) using the foods with the shortest descriptions, and ties are broken by `fdc_id`. It skips baby food, brand names and any food that is missing kcal, protein, carbs or fat. The `reason` column in the manifest says why each food was picked.

## Golden dataset

The golden dataset is a fixed set of questions with known correct answers that we will use later to test the assistant. We wrote 60 cases, 10 for each of these categories: `food_lookup`, `portion_calculation`, `daily_summary`, `constrained_suggestion`, `preference_change` and `ambiguous_unsupported`.

Each category has 6 dev cases and 4 held-out cases (marked `"split": "test"`). Cases that share a `scenario` (they are paraphrases of each other) always go in the same split, so the test set doesn't leak into the dev set. All profiles are fictional.

Every case has: `turns`, `profile_id`, `diary`, `expected_behaviour`, `expected_facts`, `acceptable_recipe_ids`, `relevant_source_ids` (`usda:<fdc_id>` / `recipe:<id>`), `required_actions`, `forbidden_actions`, `expected_db_changes`, plus `calc` / `constraints` so the expected answer can be recomputed.

```bash
python evaluation/check_golden.py   # 60 cases, Counter({'dev': 36, 'test': 24}), 0 problems
```

The checker loads the frozen data into an in-memory database and recalculates every number with `gepeto/nutrition.py` (allowed error: 1 kcal and 0.1 g). It also rebuilds the list of acceptable recipes for each suggestion from the stated constraints, and checks the counts, the split, scenario leaks and that every source id exists.

**The split is frozen from now on.** We will tune on `dev` and run `test` only once, in week 12.

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

Result (14 Nov 2026): `qwen2.5:3b`, 3.1B parameters, Q4_K_M GGUF (a compressed format that fits on a normal laptop). It loaded in about 3.8 s and returned valid JSON that chose `search_foods` for "200 g of raw chicken breast". This only shows that loading and JSON output work, it is **not** an evaluation of the model.

## Known limitations (week 7)

- Picking by "shortest description" sometimes selects unusual foods (emu, ostrich, acorns). They are valid records, just not very common ones.
- The `prep_state` guess is based on words, so for example "canned" counts as cooked.
- Recipe nutrients are **estimates**, summed from raw ingredient weights. Only 4 recipes have a `cooked_weight_g`, which is needed to log by cooked grams.
- A script checks the golden cases against the frozen data, but the second team member's manual review is still to do. We should write it down in the PR or issue before week 8 work starts.
