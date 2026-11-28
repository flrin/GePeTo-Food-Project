"""Week 9: do the development questions retrieve the expected food and recipe records?

For every dev case that needs retrieval (it requires search_foods or
search_recipes and lists relevant source ids), the question is sent to the
retrievers and the first five *records* are compared with the annotated ones
(chunks are mapped back to their record first, Milestone 2 section 7).

Two settings:

* retrieval - the raw question, only restricted to the right kind of record
  (foods or recipes). Run with dense only, BM25 only and hybrid (RRF), so the
  three can be compared on the same questions.
* tool - the question plus the case's structured constraints, through the real
  search_recipes / search_foods MCP tools, the way the assistant will call them
  in week 10. Numeric and diet rules are applied in Python there, so this is
  where "under 600 kcal" is supposed to work.

Recall@5 = relevant records in the top 5 / relevant records, averaged over the
cases. Cases with more than five acceptable recipes can't reach 1.0, so a
capped variant (divide by min(relevant, 5)) is reported next to it. Questions
with no record in the dataset (a Big Mac) are listed separately: retrieval
always returns something, and deciding that it is the wrong food is the agent's job.

The test split is not touched unless --split test is given (week 12).

    python evaluation/check_retrieval.py              # dev -> evaluation/runs/week9_retrieval.json
    python evaluation/check_retrieval.py --out -      # print only
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import anyio
from mcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evaluation.check_golden import load_cases
from evaluation.check_tools import build_database
from gepeto.config import EVALUATION_DIR
from gepeto.confirm import Confirmations
from gepeto.db import connect
from gepeto.embeddings import default_embedder
from gepeto.index import build, load_published
from gepeto.mcp_host import parse_tool_error
from gepeto.mcp_server import build_server
from gepeto.profiles import save_profile
from gepeto.retrieval import KEEP, METHODS, Filters, HybridRetriever
from gepeto.tools import ToolContext

SEARCH_TOOLS = {"search_foods": "food", "search_recipes": "recipe"}
RULE_KEYS = ("meal_type", "diet", "require_tag", "exclude_ingredients", "max_kcal", "min_protein_g")


def retrieval_kind(case: dict) -> str | None:
    kinds = {"food" if ref.startswith("usda:") else "recipe" for ref in case["relevant_source_ids"]}
    needed = {SEARCH_TOOLS[a] for a in case["required_actions"] if a in SEARCH_TOOLS}
    return kinds.pop() if len(kinds) == 1 and kinds <= needed else None


def question(case: dict) -> str:
    """The turn that asks for something to be looked up: the last one."""
    return case["turns"][-1]


def scores(top: list[str], relevant: list[str]) -> dict:
    hits = [ref for ref in relevant if ref in top[:KEEP]]
    first = next((n for n, ref in enumerate(top, start=1) if ref in relevant), None)
    return {
        "recall_at_5": len(hits) / len(relevant),
        "capped_recall_at_5": len(hits) / min(len(relevant), KEEP),
        "hit_at_5": bool(hits),
        "reciprocal_rank": 0.0 if first is None or first > KEEP else 1 / first,
        "missed": [ref for ref in relevant if ref not in top[:KEEP]],
    }


def mean(values) -> float:
    values = list(values)
    return round(sum(values) / len(values), 4) if values else 0.0


def summarize(rows: list[dict]) -> dict:
    return {
        "cases": len(rows),
        "recall_at_5": mean(r["recall_at_5"] for r in rows),
        "capped_recall_at_5": mean(r["capped_recall_at_5"] for r in rows),
        "hit_at_5": f"{sum(r['hit_at_5'] for r in rows)}/{len(rows)}",
        "mrr_at_5": mean(r["reciprocal_rank"] for r in rows),
        "relevant_found": f"{sum(len(r['relevant']) - len(r['missed']) for r in rows)}/"
                          f"{sum(len(r['relevant']) for r in rows)}",
    }


async def tool_results(case: dict, profile: dict, db_path: Path, index_dir: Path, kind: str) -> dict:
    conn = connect(db_path)
    profile_id = save_profile(conn, {k: v for k, v in profile.items() if k != "id"})["id"]
    conn.close()
    ctx = ToolContext(db_path, profile_id, Confirmations("evaluation-only-secret"), index_dir=index_dir)
    if kind == "recipe":
        name, args = "search_recipes", {"query": question(case), "limit": KEEP}
        rules = case.get("constraints", {})
        args.update({key: rules[key] for key in RULE_KEYS if rules.get(key) is not None})
    else:
        name, args = "search_foods", {"query": question(case), "limit": KEEP}
    async with Client(build_server(ctx)) as client:
        result = await client.call_tool(name, args)
    if result.is_error:
        raise parse_tool_error(" ".join(part.text for part in result.content))
    content = result.structured_content
    return {"tool": name, "arguments": args, "retrieval": content["retrieval"],
            "top": [r["source_id"] for r in content["results"]],
            "total_matches": content.get("total_matches")}


async def run(split: str = "dev") -> dict:
    profiles = {p["id"]: p for p in json.loads((EVALUATION_DIR / "profiles.json").read_text(encoding="utf-8"))}
    cases = [c for c in load_cases() if split == "all" or c["split"] == split]
    embedder = default_embedder()
    with tempfile.TemporaryDirectory() as tmp:
        db_path, index_dir = Path(tmp) / "eval.db", Path(tmp) / "indexes"
        build_database(db_path)
        conn = connect(db_path)
        manifest = build(conn, index_dir, embedder)
        conn.close()
        retriever = HybridRetriever(load_published(index_dir), embedder)

        results, no_record = [], []
        for case in cases:
            kind = retrieval_kind(case)
            searches = [a for a in case["required_actions"] if a in SEARCH_TOOLS]
            if kind is None:
                if searches and not case["relevant_source_ids"]:
                    hits = retriever.search(question(case), Filters(SEARCH_TOOLS[searches[0]]), keep=3)
                    no_record.append({"id": case["id"], "question": question(case),
                                      "top": [{"record_id": h.record_id, "title": retriever.version.chunk_text(h.chunk_ids[0]).split("\n")[0],
                                               "similarity": None if h.similarity is None else round(h.similarity, 3),
                                               "matched_terms": h.matched_terms} for h in hits]})
                continue
            row = {"id": case["id"], "category": case["category"], "kind": kind, "question": question(case),
                   "relevant": case["relevant_source_ids"], "methods": {}}
            for method in METHODS:
                top = [h.record_id for h in retriever.search(question(case), Filters(kind), keep=10, method=method)]
                row["methods"][method] = {"top5": top[:KEEP], **scores(top, case["relevant_source_ids"]),
                                          "rank_in_top10": {ref: (top.index(ref) + 1 if ref in top else None)
                                                            for ref in case["relevant_source_ids"]}}
            tool = await tool_results(case, profiles[case["profile_id"]], db_path, index_dir, kind)
            acceptable = case["acceptable_recipe_ids"]
            tool.update(scores(tool["top"], case["relevant_source_ids"]))
            if acceptable:
                tool["all_results_acceptable"] = all(ref.split(":", 1)[1] in acceptable for ref in tool["top"])
            row["tool"] = tool
            results.append(row)

    summary = {
        "retrieval": {method: summarize([{**r["methods"][method], "relevant": r["relevant"]} for r in results])
                      for method in METHODS},
        "retrieval_by_kind": {
            kind: {method: summarize([{**r["methods"][method], "relevant": r["relevant"]}
                                      for r in results if r["kind"] == kind]) for method in METHODS}
            for kind in ("food", "recipe")
        },
        "tool": summarize([{**r["tool"], "relevant": r["relevant"]} for r in results]),
        "tool_all_results_acceptable": f"{sum(r['tool'].get('all_results_acceptable', True) for r in results if 'all_results_acceptable' in r['tool'])}/"
                                       f"{sum('all_results_acceptable' in r['tool'] for r in results)}",
        "tool_methods": sorted({r["tool"]["retrieval"]["method"] for r in results}),
    }
    return {
        "split": split,
        "index": {k: manifest[k] for k in ("model", "dimensions", "distance", "chunking", "retrieval", "bm25",
                                           "records", "chunks", "chunk_parts", "max_chunk_tokens")},
        "summary": summary,
        "results": results,
        "no_record_in_dataset": no_record,
    }


def print_report(report: dict) -> None:
    s = report["summary"]
    print(f"{s['retrieval']['hybrid']['cases']} {report['split']} cases need retrieval "
          f"({report['index']['records']} records, {report['index']['chunks']} chunks, "
          f"max {report['index']['max_chunk_tokens']} tokens)")
    print(f"{'setting':<18}{'Recall@5':>10}{'capped':>8}{'Hit@5':>8}{'MRR@5':>8}{'found':>9}")
    rows = [(f"question/{m}", s["retrieval"][m]) for m in METHODS] + [("tool (hybrid)", s["tool"])]
    for name, row in rows:
        print(f"{name:<18}{row['recall_at_5']:>10.3f}{row['capped_recall_at_5']:>8.3f}{row['hit_at_5']:>8}"
              f"{row['mrr_at_5']:>8.3f}{row['relevant_found']:>9}")
    for kind, methods in s["retrieval_by_kind"].items():
        print(f"  {kind:<7} " + "  ".join(f"{m} {methods[m]['recall_at_5']:.3f}" for m in METHODS))
    print(f"tool results all acceptable: {s['tool_all_results_acceptable']}; tool methods: {s['tool_methods']}")
    for r in report["results"]:
        missed = r["methods"]["hybrid"]["missed"]
        if missed:
            print(f"  hybrid missed {r['id']}: {missed} ranks {r['methods']['hybrid']['rank_in_top10']}")
        if r["tool"]["missed"]:
            print(f"  tool missed   {r['id']}: {r['tool']['missed']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    parser.add_argument("--out", default=str(EVALUATION_DIR / "runs" / "week9_retrieval.json"))
    args = parser.parse_args()
    report = anyio.run(run, args.split)
    if args.out != "-":
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print_report(report)


if __name__ == "__main__":
    main()
