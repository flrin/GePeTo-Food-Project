"""Week 9: chunking, the Chroma + BM25 indexes, hybrid retrieval and its use in the search tools."""

import anyio
import numpy as np
import pytest

from gepeto import tools
from gepeto.chunks import CHUNK_TOKENS, OVERLAP_TOKENS, build_chunks, recipe_chunks
from gepeto.db import connect
from gepeto.embeddings import default_embedder
from gepeto.importer import food_id_for, upsert_foods, upsert_recipes
from gepeto.index import CURRENT, IndexBuildError, build, live, load_published, published_name
from gepeto.retrieval import Filters, HybridRetriever, bm25_tokens, collapse, rrf
from gepeto.tools import ToolContext

from test_diary_numbers import FOODS


@pytest.fixture(scope="module")
def retriever(index_dir):
    return HybridRetriever(load_published(index_dir), default_embedder())


def top(retriever, query, kind="food", n=5, **kwargs):
    return [hit.record_id for hit in retriever.search(query, Filters(kind, **kwargs), keep=n)]


# ------------------------------------------------------------------ chunking

def test_chunks_fit_200_embedding_tokens_and_foods_are_never_merged(conn) -> None:
    embedder = default_embedder()
    chunks = build_chunks(conn, embedder)
    assert all(embedder.count_tokens(c.text) == c.tokens <= CHUNK_TOKENS for c in chunks)
    foods = [c for c in chunks if c.kind == "food"]
    assert len(foods) == len({c.record_id for c in foods}) == 300  # one card per food
    for row in conn.execute("SELECT id, title FROM recipes"):
        parts = [c for c in chunks if c.record_id == f"recipe:{row['id']}"]
        assert parts and all(c.text.startswith(row["title"]) for c in parts)  # the title is in every chunk
        names = [r["name"] for r in conn.execute("SELECT name FROM recipe_ingredients WHERE recipe_id = ?", (row["id"],))]
        assert all(name in parts[0].text for name in names)  # the ingredient list stays together


def long_recipe(conn, recipe_id: str, steps: list[str], ingredients: int = 3) -> None:
    rows = conn.execute("SELECT source_id FROM foods ORDER BY id LIMIT ?", (ingredients,)).fetchall()
    report = upsert_recipes(conn, [{
        "id": recipe_id, "title": "Slow test stew", "meal_type": "dinner", "servings": 4, "weight_state": "raw",
        "tags": ["vegan"], "steps": steps,
        "ingredients": [{"fdc_id": r["source_id"], "name": f"ingredient {n}", "grams": 10} for n, r in enumerate(rows)],
    }])
    assert report.rejected == []


def test_long_instructions_split_at_step_boundaries_with_overlap(conn) -> None:
    embedder = default_embedder()
    steps = [f"Step about the {word} part: stir the pot gently, taste it, and wait for the {word} to soften."
             for word in "onion carrot celery garlic tomato bean pepper leek squash potato kale herb".split()] * 2
    long_recipe(conn, "r90", steps)
    chunks = recipe_chunks(conn, conn.execute("SELECT * FROM recipes WHERE id = 'r90'").fetchone(), embedder)
    assert chunks[0].part == "ingredients" and "Steps" not in chunks[0].text
    steps_chunks = chunks[1:]
    assert len(steps_chunks) >= 2 and all(c.part == "steps" and c.tokens <= CHUNK_TOKENS for c in steps_chunks)
    for before, after in zip(steps_chunks, steps_chunks[1:]):
        tail = embedder.token_ids(before.text.split("steps:\n", 1)[1])[-OVERLAP_TOKENS:]
        head = embedder.token_ids(after.text.split("steps:\n", 1)[1])
        assert embedder.decode(tail) in embedder.decode(head[: OVERLAP_TOKENS + 5])
    # Every step (as the tokenizer sees it) is in some chunk.
    joined = " ".join(c.text for c in steps_chunks)
    assert all(embedder.decode(embedder.token_ids(f"{n}. {step}")) in joined for n, step in enumerate(steps, 1))


def test_an_oversized_ingredient_list_becomes_a_summary(conn) -> None:
    long_recipe(conn, "r91", ["Mix everything."], ingredients=60)
    chunks = recipe_chunks(conn, conn.execute("SELECT * FROM recipes WHERE id = 'r91'").fetchone(), default_embedder())
    assert chunks[0].part == "summary" and chunks[0].tokens <= CHUNK_TOKENS
    assert "more." in chunks[0].text and "full list loaded by recipe id" in chunks[0].text


# ------------------------------------------------------------------ fusion

def test_reciprocal_rank_fusion_uses_1_over_60_plus_rank() -> None:
    scores = rrf([["a", "b", "c"], ["c", "a"]])
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["b"] == pytest.approx(1 / 62)
    assert scores["c"] == pytest.approx(1 / 63 + 1 / 61)


def test_a_record_counts_once_per_list() -> None:
    ranked = [("recipe:r01#1", "recipe:r01", 0.9), ("usda:1", "usda:1", 0.8), ("recipe:r01#0", "recipe:r01", 0.7)]
    assert collapse(ranked) == [("recipe:r01", 0.9, ["recipe:r01#1", "recipe:r01#0"]), ("usda:1", 0.8, ["usda:1"])]


def test_bm25_tokens_fold_plurals_and_drop_numbers() -> None:
    assert bm25_tokens("200 g of Strawberries and 2 eggs, whole-wheat") == ["strawberry", "egg", "whole", "wheat"]


# ------------------------------------------------------------------ retrieval

@pytest.mark.parametrize("query, expected", [
    ("raw chicken breast", "usda:171077"),
    ("olive oil", "usda:171413"),
    ("feta cheese", "usda:173420"),
    ("dry oats", "usda:173904"),
])
def test_known_foods_come_first(retriever, query, expected) -> None:
    assert top(retriever, query)[0] == expected


def test_recipes_are_found_by_dish_name(retriever) -> None:
    assert top(retriever, "lentil soup", kind="recipe")[0] == "recipe:r08"
    assert top(retriever, "chicken with rice and broccoli", kind="recipe")[0] == "recipe:r07"


def test_chroma_dense_ranking_equals_exact_cosine(retriever) -> None:
    """HNSW is approximate; on 330 chunks with ef_search=200 it must agree with brute force."""
    stored = retriever.version.collection.get(include=["embeddings", "metadatas"])
    ids, vectors = stored["ids"], np.asarray(stored["embeddings"])
    food = [i for i, meta in enumerate(stored["metadatas"]) if meta["kind"] == "food"]
    for query in ("raw chicken breast", "plain lowfat greek yogurt", "something sweet for breakfast"):
        q = default_embedder().encode([query])[0]
        exact = sorted(food, key=lambda i: -float(vectors[i] @ q))[:10]
        assert [c for c, _, _ in retriever.dense(query, Filters("food"))] == [ids[i] for i in exact]


def test_filters_apply_inside_both_retrievers(retriever) -> None:
    cooked = Filters("food", equals={"prep_state": "cooked"})
    found = retriever.dense("chicken", cooked) + retriever.keyword("chicken", cooked)
    assert found
    for chunk_id, *_ in found:
        assert retriever.version.collection.get(ids=[chunk_id])["metadatas"][0]["prep_state"] == "cooked"
    allowed = frozenset({"recipe:r06", "recipe:r24"})
    hits = retriever.search("chicken", Filters("recipe", record_ids=allowed), keep=5)
    assert {h.record_id for h in hits} <= allowed
    assert retriever.search("chicken", Filters("recipe", record_ids=frozenset()), keep=5) == []


def test_same_query_same_order(retriever) -> None:
    runs = [[(h.record_id, h.score) for h in retriever.search("vegetarian dinner", Filters("recipe"))] for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]


# ------------------------------------------------------------------ the search tools

def test_search_foods_tool_uses_the_hybrid_index(ctx) -> None:
    result = tools.search_foods(ctx, "plain lowfat greek yogurt")
    assert result["retrieval"]["method"] == "hybrid" and result["retrieval"]["index_up_to_date"] is True
    best = result["results"][0]
    assert best["source_id"] == "usda:170903" and best["per_100g"]["protein_g"] == 9.95
    assert best["match"]["dense_rank"] and best["match"]["bm25_rank"] and "greek" in best["match"]["matched_terms"]


@pytest.mark.parametrize("query", ["mushroom omelette", "chicken", "creamy pasta with cheese", "steak", "shrimp"])
def test_hybrid_ranking_never_returns_a_recipe_that_breaks_a_rule(db_path, make_profile, confirmations, index_dir, query) -> None:
    dan = ToolContext(db_path, make_profile("p_dan"), confirmations, index_dir=index_dir)  # pescatarian, no mushrooms
    result = tools.search_recipes(dan, query=query, max_kcal=600, limit=10)
    assert result["retrieval"]["method"] == "hybrid" and result["retrieval"]["recheck_dropped"] == []
    for card in result["results"]:
        assert set(card["tags"]) & {"pescatarian", "vegetarian", "vegan"} and card["per_serving"]["kcal"] <= 600
        assert not any("mushroom" in i["name"] for i in card["ingredients"])


def test_ranking_only_reorders_what_the_rules_allow(ctx) -> None:
    rules = {"meal_type": "dinner", "diet": "vegetarian", "max_kcal": 450, "exclude_ingredients": ["mushroom"]}
    plain = tools.search_recipes(ctx, limit=10, **rules)
    ranked = tools.search_recipes(ctx, query="vegetarian dinner", limit=10, **rules)
    assert plain["retrieval"]["method"] == "filters_only"
    assert sorted(r["recipe_id"] for r in ranked["results"]) == sorted(r["recipe_id"] for r in plain["results"])


def test_without_an_index_the_tools_fall_back_and_say_so(db_path, make_profile, confirmations, tmp_path) -> None:
    bare = ToolContext(db_path, make_profile("p_ana"), confirmations, index_dir=tmp_path / "empty")
    result = tools.search_foods(bare, "raw chicken breast")
    assert result["retrieval"]["method"] == "keyword_fallback" and "gepeto.index build" in result["retrieval"]["reason"]
    assert result["results"][0]["source_id"] == "usda:171077"


def test_index_older_than_the_data_is_flagged_but_numbers_are_current(ctx, conn) -> None:
    upsert_foods(conn, [{**FOODS["171077"], "kcal": 150.0}])
    result = tools.search_foods(ctx, "raw chicken breast")
    assert result["retrieval"]["index_up_to_date"] is False
    assert result["results"][0]["per_100g"]["kcal"] == 150.0  # from SQLite, not from the indexed text


# ------------------------------------------------------------------ build and publish

def test_build_publishes_only_complete_versions(conn, tmp_path, fake_embedder) -> None:
    index_dir = tmp_path / "idx"
    first = build(conn, index_dir, fake_embedder)
    assert published_name(index_dir) == "v0001" and first["chunks"] == 330 and first["embedded"] == 330
    reader = live(index_dir)
    assert reader.current().name == "v0001"

    upsert_foods(conn, [{**FOODS["171077"], "kcal": 150.0}])
    with pytest.raises(IndexBuildError):
        build(conn, index_dir, fake_embedder, _fail_before_publish=True)
    assert (index_dir / CURRENT).read_text().strip() == "v0001"     # readers never saw v0002
    assert reader.current().name == "v0001" and reader.current().collection.count() == 330

    second = build(conn, index_dir, fake_embedder)
    assert second["version"] == "v0003" and reader.current().name == "v0003"
    assert not (index_dir / "v0002").exists()                       # the failed build was cleaned up


def test_rebuild_only_embeds_changed_chunks_and_drops_superseded_ones(conn, tmp_path, fake_embedder) -> None:
    index_dir = tmp_path / "idx"
    build(conn, index_dir, fake_embedder)
    assert build(conn, index_dir, fake_embedder)["unchanged"] is True and published_name(index_dir) == "v0001"

    fake_embedder.encoded = 0
    upsert_foods(conn, [{**FOODS["171077"], "kcal": 150.0}])
    chicken = food_id_for(conn, "171077")
    using = {r[0] for r in conn.execute("SELECT DISTINCT recipe_id FROM recipe_ingredients WHERE food_id = ?", (chicken,))}
    manifest = build(conn, index_dir, fake_embedder)
    # The chicken card changed, and so did the per-serving line of every recipe that uses it.
    assert manifest["embedded"] == fake_embedder.encoded == 1 + len(using)
    assert manifest["superseded"] == 1 + len(using) and manifest["reused"] == 330 - manifest["embedded"]
    version = load_published(index_dir)
    stored = version.collection.get(ids=["usda:171077"], include=["documents"])["documents"][0]
    assert "150 kcal" in stored and version.collection.count() == 330


def test_old_versions_are_pruned_but_the_previous_one_is_kept(conn, tmp_path, fake_embedder) -> None:
    index_dir = tmp_path / "idx"
    for _ in range(4):
        build(conn, index_dir, fake_embedder, force=True)
    assert sorted(p.name for p in index_dir.iterdir() if p.is_dir()) == ["v0003", "v0004"]


def test_a_newly_imported_food_is_searchable_after_the_rebuild(db_path, ctx, conn, tmp_path) -> None:
    """The importer's step 4, end to end with the real model."""
    index_dir = tmp_path / "idx"
    build(conn, index_dir)
    upsert_foods(conn, [{"source": "user", "source_id": "label-0001", "description": "Kefir, plain, 2% fat (label)",
                         "category": "Dairy and Egg Products", "prep_state": "unspecified", "kcal": 52, "protein_g": 3.4,
                         "carbs_g": 4.7, "fat_g": 2.0, "source_version": "label 2026-11", "user_entered": True,
                         "source_note": "typed in from the package"}])
    fresh = ToolContext(db_path, ctx.profile_id, ctx.confirmations, index_dir=index_dir)
    assert tools.search_foods(fresh, "kefir")["retrieval"]["index_up_to_date"] is False
    manifest = build(conn, index_dir)
    assert manifest["embedded"] == 1 and manifest["records"] == 331
    best = tools.search_foods(fresh, "kefir")["results"][0]
    assert best["source_id"] == "user:label-0001" and best["user_entered"] is True


# ------------------------------------------------------------------ dev retrieval results

def test_dev_questions_retrieve_their_records() -> None:
    from evaluation.check_retrieval import run
    report = anyio.run(run, "dev")
    s = report["summary"]
    assert s["retrieval"]["hybrid"]["cases"] == 21
    assert s["retrieval"]["hybrid"]["hit_at_5"] == "21/21"
    assert s["retrieval"]["hybrid"]["recall_at_5"] >= 0.85
    assert s["tool"]["capped_recall_at_5"] >= 0.95 and s["tool_all_results_acceptable"] == "8/8"
    assert s["tool_methods"] == ["hybrid"]
