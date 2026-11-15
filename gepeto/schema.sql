
CREATE TABLE IF NOT EXISTS foods (
    id             INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    description    TEXT NOT NULL,
    category       TEXT,
    prep_state     TEXT NOT NULL CHECK (prep_state IN ('raw', 'cooked', 'unspecified')),
    kcal           REAL CHECK (kcal IS NULL OR kcal >= 0),
    protein_g      REAL CHECK (protein_g IS NULL OR protein_g >= 0),
    carbs_g        REAL CHECK (carbs_g IS NULL OR carbs_g >= 0),
    fat_g          REAL CHECK (fat_g IS NULL OR fat_g >= 0),
    source_version TEXT NOT NULL,
    import_date    TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    version        INTEGER NOT NULL DEFAULT 1,
    user_entered   INTEGER NOT NULL DEFAULT 0,
    source_note    TEXT,
    UNIQUE (source, source_id)
);

CREATE TABLE IF NOT EXISTS recipes (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    meal_type       TEXT NOT NULL,
    servings        INTEGER NOT NULL CHECK (servings > 0),
    weight_state    TEXT NOT NULL CHECK (weight_state IN ('raw', 'cooked')),
    cooked_weight_g REAL CHECK (cooked_weight_g IS NULL OR cooked_weight_g > 0),
    tags            TEXT NOT NULL DEFAULT '[]',
    steps           TEXT NOT NULL DEFAULT '[]',
    content_hash    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipe_ingredients (
    recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    food_id   INTEGER NOT NULL REFERENCES foods(id),
    grams     REAL NOT NULL CHECK (grams > 0),
    PRIMARY KEY (recipe_id, food_id)
);

CREATE TABLE IF NOT EXISTS profiles (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    sex             TEXT CHECK (sex IN ('female', 'male')),
    age             INTEGER CHECK (age IS NULL OR age > 0),
    height_cm       REAL CHECK (height_cm IS NULL OR height_cm > 0),
    weight_kg       REAL CHECK (weight_kg IS NULL OR weight_kg > 0),
    activity_factor REAL,
    goal            TEXT,
    kcal_target     REAL,
    protein_target_g REAL,
    carbs_target_g  REAL,
    fat_target_g    REAL,
    preferences     TEXT NOT NULL DEFAULT '[]',
    restrictions    TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS meal_logs (
    id           INTEGER PRIMARY KEY,
    profile_id   INTEGER NOT NULL REFERENCES profiles(id),
    log_date     TEXT NOT NULL,
    request_id   TEXT NOT NULL UNIQUE,
    food_id      INTEGER REFERENCES foods(id),
    food_version INTEGER,
    recipe_id    TEXT REFERENCES recipes(id),
    grams        REAL CHECK (grams IS NULL OR grams > 0),
    servings     REAL CHECK (servings IS NULL OR servings > 0),
    kcal         REAL,
    protein_g    REAL,
    carbs_g      REAL,
    fat_g        REAL,
    created_at   TEXT NOT NULL,
    CHECK ((food_id IS NULL) <> (recipe_id IS NULL))
);

CREATE TABLE IF NOT EXISTS import_runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    source      TEXT NOT NULL,
    inserted    INTEGER NOT NULL,
    updated     INTEGER NOT NULL,
    unchanged   INTEGER NOT NULL,
    rejected    INTEGER NOT NULL
);
