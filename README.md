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

## Week 8 (16-22 Nov): profiles, diary, MCP tools and the form UI

What the plan asked for this week: *Implement profile/target setup, diary and nutrient calculations. Expose the core MCP tools and connect the UI. Run deterministic numeric and duplicate-write tests.*

| What we did | Where it is |
| --- | --- |
| Target estimate (Mifflin-St Jeor, activity factor, goal) | [`gepeto/targets.py`](gepeto/targets.py) |
| Profile setup, editable targets, confirmed preference changes | [`gepeto/profiles.py`](gepeto/profiles.py) |
| Diary: meal preview, saving a confirmed preview once, daily summary | [`gepeto/diary.py`](gepeto/diary.py) |
| Keyword food search and rule-based recipe filters (baseline until week 9) | [`gepeto/search.py`](gepeto/search.py) |
| The seven core tools as plain functions | [`gepeto/tools.py`](gepeto/tools.py) |
| MCP server (official SDK, stdio) | [`gepeto/mcp_server.py`](gepeto/mcp_server.py) |
| Tool schemas (input and output JSON schemas, as a client sees them) | [`docs/tool_schemas.json`](docs/tool_schemas.json), made by [`scripts/export_tool_schemas.py`](scripts/export_tool_schemas.py) |
| FastAPI app, which is also the MCP host/client | [`gepeto/api.py`](gepeto/api.py), [`gepeto/mcp_host.py`](gepeto/mcp_host.py) |
| Confirmation tokens (only the app can approve a write) | [`gepeto/confirm.py`](gepeto/confirm.py) |
| Form UI: profile and targets, logging meals, day view | [`ui/app.py`](ui/app.py) |
| Golden dev cases run through the real MCP tools | [`evaluation/check_tools.py`](evaluation/check_tools.py), output in [`evaluation/runs/week8_tools.json`](evaluation/runs/week8_tools.json) |
| Numeric, duplicate-write, MCP, API and UI tests | [`tests/`](tests/), output in [`evaluation/runs/week8_pytest.txt`](evaluation/runs/week8_pytest.txt) |

### Run it

```bash
python -m gepeto.importer foods data/foods_300.jsonl      # or the USDA import above
python -m gepeto.importer recipes data/recipes.json
python scripts/seed_profiles.py                           # optional: the 5 fictional profiles

uvicorn gepeto.api:app --port 8000                        # terminal 1: API + MCP host
streamlit run ui/app.py                                   # terminal 2: the form UI
```

If you already have a `gepeto.db` from week 7, it gets upgraded automatically the first time it's opened. After that, run `importer recipes` one more time so the recipes get their ingredient names (we need them for filters like "no mushrooms").

### How the parts connect

```mermaid
flowchart LR
    UI[Streamlit form] -->|HTTP| API[FastAPI]
    API -->|profile form, target estimate| SQL[(SQLite)]
    API -->|MCP client, stdio| MCP[MCP server, one process per profile]
    MCP --> TOOLS[gepeto/tools.py] --> SQL
```

The profile form talks to FastAPI directly, since only the user fills it in. Everything the assistant will also do in week 10 (searching, calculating, logging, summarizing) already goes through the MCP server. This way the form tests the same tools, schemas and protections that the chat will use later.

FastAPI starts the MCP server as a subprocess and gives it the profile id and a shared secret through the environment. **No tool takes a user or profile id as an argument**, so the model can't ask about someone else's data. There's a test that checks this on the exported schemas.


### Logging flow

1. **Search** for a food (`search_foods`) or pick a recipe.
2. **Calculate** (`calculate_meal`). It returns calories and macros, unknown fields, rule checks (diet, disliked ingredients, daily kcal target) and a `preview_id`. The preview is stored, but it is added to the diary yet.
3. **Save.** When the user clicks *Save to diary*, FastAPI signs `(profile, preview_id)` with the secret (HMAC-SHA256) and calls `log_meal` with that token and a `request_id`. The model never sees the secret, so it can't confirm its own writes. `update_preferences` works the same way.
4. **Day view** (`get_daily_summary`): the meals, totals, targets and how much is left.

Recipes can be logged by servings, or by cooked grams if the recipe has a `cooked_weight_g`. If it doesn't, the tool says it can't (`missing_data`) instead of guessing.

### MCP tools

| Tool | Writes? | Notes |
| --- | --- | --- |
| `get_profile` | no | Targets, preferences, restrictions. Leaves out age, height and weight on purpose. |
| `get_daily_summary` | no | Unknown values stay `null`, never `0`. |
| `search_foods` | no | Keyword baseline for now. Week 9 replaces the ranking with hybrid retrieval. |
| `search_recipes` | no | Hard rules in Python: diet, excluded ingredients, kcal/protein per serving, "fits the rest of my day". The profile's own restrictions always apply. |
| `calculate_meal` | preview only | Foods by grams, or recipes by servings or cooked grams. |
| `log_meal` | yes | Needs an app-issued token. Saving the same thing again never duplicates it. |
| `update_preferences` | yes | Needs a token issued by the app. Repeating the same change does nothing bad. |


Errors have one of these codes: `invalid_input`, `not_found`, `missing_data`, `unconfirmed`, `conflict`, `stale_preview` or `unavailable`. The app uses them to tell a bad quantity apart from missing data or a service that is down.

### How we stop duplicate or unconfirmed writes

- `meal_logs` has `UNIQUE (request_id, item_index)`. If you save with the same `request_id` again, you get `already_logged` and no new rows.
- A preview can only be saved once. If the same preview comes back with a new `request_id` (for example when the model retries), it's reported as already logged.
- Using the same `request_id` for a different meal gives a `conflict`.
- Saving runs inside `BEGIN IMMEDIATE`, so two writers can't both pass the checks.
- If the food data changed after the preview was shown, the preview becomes `stale_preview` and has to be calculated again. Meals that were already saved keep their own nutrient snapshot.
- A meal with several items is saved completely or not at all.
- The UI makes one `request_id` per preview, so double-clicking *Save* or a Streamlit rerun can't save a meal twice.

### Results

```bash
pytest                                # 106 passed, 1 skipped (Ollama)
python evaluation/check_tools.py      # 28 dev cases: numeric 32/32, suggestions 8/8, db changes 4/4
```

`check_tools.py` takes every **dev** golden case that has numbers, recipe constraints or expected database changes and runs it through an MCP client session. The starting diary is created with `calculate_meal` + `log_meal`, the numbers come from `calculate_meal` / `get_daily_summary`, the suggestions from `search_recipes`, and confirmed changes go through `update_preferences`. The tolerance is the same as in week 7 (1 kcal and 0.1 g).

In [`tests/test_diary_numbers.py`](tests/test_diary_numbers.py) the expected values are computed by hand or directly from the JSON data files, not with `gepeto/nutrition.py`. This way a bug in our own arithmetic can't make a test pass by mistake.

### Target estimate

```
resting energy = 10 x kg + 6.25 x cm - 5 x age + 5 (male) / - 161 (female)    [Mifflin-St Jeor]
kcal target    = resting energy x activity factor (1.2 - 1.9) x goal (lose 0.85 / maintain 1.0 / gain 1.10)
protein        = 1.6 g/kg (1.8 g/kg when losing), fat = 30 % of kcal, carbs = the rest
```

The estimate is only available for adults (18-100). If an input is missing, the form tells you which one, and you can type the targets yourself instead. The profile remembers if its targets are the `estimate` or `manual`, and any edit turns them into `manual`. The language model never calls the estimate, and it can only change a target through a confirmed `update_preferences`.

### Known limitations (week 8)

- `search_foods` is a plain keyword match (for example "greek yogurt" also returns tofu yogurt). Fixing this is the job of week 9.
- A food's diet check comes from its USDA category. Mixed categories like *Fats and Oils* or *Baked Products* return `unknown`, not `pass`. We don't have allergen data, so a recipe that doesn't contain the word "shrimp" is not proof that it has no shrimp.
- Free-text restrictions we don't understand (for example "low sodium") are not silently ignored. They come back as `unapplied_restrictions`.
- There's no login, because this is a local prototype. The profile selected in the sidebar is the session, and only fictional profiles should be used.
- Previews that were never saved get deleted after a day.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # MCP SDK, FastAPI, Streamlit (pinned in pyproject.toml) + pytest
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
- `meal_logs` store a snapshot of the nutrients and a `food_version`, so importing new data later can't change totals from earlier days.
- `request_id` is `UNIQUE`, so the same request can't log a meal twice. (Since week 8 a meal can have several items, so the constraint is `UNIQUE (request_id, item_index)`.)

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
pytest                                   # 106 tests, model test skipped (output: evaluation/runs/week8_pytest.txt)
GEPETO_RUN_MODEL_TESTS=1 pytest          # + live Ollama test
```

## Model smoke test

```bash
ollama pull qwen2.5:3b
python -m gepeto.smoke_model --out evaluation/runs/smoke_model.json
```

Result (14 Nov 2026): we used `qwen2.5:3b`, a small model that runs on a normal laptop. It loaded in about 3.8 s and gave back valid JSON that picked `search_foods` for "200 g of raw chicken breast". This only shows that the model loads and can output JSON, it is **not** an evaluation of the model.

## Known limitations (week 7)

- Picking by "shortest description" sometimes selects unusual foods (emu, ostrich, acorns). They are valid records, just not very common ones.
- The `prep_state` guess is based on words, so for example "canned" counts as cooked.
- Recipe nutrients are **estimates**, summed from raw ingredient weights. Only 4 recipes have a `cooked_weight_g`, which is needed to log by cooked grams.
- The golden cases are checked by a script against the frozen data.
