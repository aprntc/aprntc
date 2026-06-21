"""A/B retrieval evaluation — keyword KB vs semantic (VikingDB) KB on the GOLD set.

Runs the 20-question BytePlus GOLD set against both retrievers and reports
**recall@k**: did a chunk citing the expected doc area appear in the top-k results?
Prints per-question hits/misses and a final side-by-side summary table.

Read-only — calls VikingDB for the semantic side but never upserts or mutates.

  .venv/bin/python scripts/eval_byteplus_retrieval.py             # k=4 (the default agent retrieves k=4)
  .venv/bin/python scripts/eval_byteplus_retrieval.py --k 8       # bigger top-k
  .venv/bin/python scripts/eval_byteplus_retrieval.py --keyword-only  # skip semantic (no VikingDB needed)
"""

from __future__ import annotations

import argparse
import sys

from aprntc.config import Settings
from aprntc.demos.byteplus.gold import GOLD
from aprntc.demos.byteplus.kb import build_kb
from aprntc.demos.byteplus.kb_semantic import SemanticKnowledgeBase


def _matches_expected_doc(chunk_doc: str, chunk_section: str, expect_doc: str) -> bool:
    """Does this chunk come from the doc area the gold question points at?

    Match is case-insensitive substring on EITHER the doc name OR the section
    heading — the gold spec uses both kinds of pointers (e.g. "Deepreasoning"
    is a doc name, "Create Index" is a section).
    """
    needle = expect_doc.lower().strip()
    return needle in chunk_doc.lower() or needle in chunk_section.lower()


def _eval_retriever(name: str, retriever, gold, k: int):
    """Run all questions against one retriever; return per-question + aggregate."""
    results = []
    hits = 0
    for q in gold:
        chunks = retriever.search(q.question, k=k)
        ok = any(_matches_expected_doc(c.doc, c.section, q.expect_doc) for c in chunks)
        if ok:
            hits += 1
        results.append({
            "question": q.question, "expect_doc": q.expect_doc, "hit": ok,
            "top_doc": chunks[0].doc if chunks else None,
            "top_section": chunks[0].section if chunks else None,
        })
    return results, hits


def _print_per_question(label: str, results: list[dict]) -> None:
    print(f"\n--- {label} ---")
    for r in results:
        mark = "✓" if r["hit"] else "✗"
        top = f"{r['top_doc']} › {r['top_section']}" if r["top_doc"] else "(no hits)"
        print(f"  {mark}  {r['question'][:55]:55s}  → top: {top[:60]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--k", type=int, default=4, help="top-k for recall@k (default: 4)")
    parser.add_argument("--keyword-only", action="store_true",
                        help="skip the semantic side (no VikingDB connection needed)")
    parser.add_argument("--collection", default=None)
    parser.add_argument("--index", default=None)
    args = parser.parse_args()

    print(f"GOLD set: {len(GOLD)} questions, recall@{args.k}")
    print(f"BytePlus KB: building …")
    kb_keyword = build_kb()
    print(f"  ↳ {len(kb_keyword)} chunks across {len(kb_keyword.docs())} doc areas")

    print("\n[KEYWORD KB] retrieving …")
    kw_results, kw_hits = _eval_retriever("keyword", kb_keyword, GOLD, args.k)

    if not args.keyword_only:
        print("[SEMANTIC KB] connecting to VikingDB …")
        try:
            settings = Settings.from_env(dotenv=".env")
            settings.vikingdb.validate()
        except Exception as e:
            print(f"  ↳ VikingDB unavailable ({e}); falling back to keyword-only.", file=sys.stderr)
            args.keyword_only = True

    if not args.keyword_only:
        kwargs = {}
        if args.collection: kwargs["collection"] = args.collection
        if args.index: kwargs["index"] = args.index
        skb = SemanticKnowledgeBase(settings.vikingdb, **kwargs)
        print(f"  ↳ collection: {skb.collection}")
        print("[SEMANTIC KB] retrieving …")
        sem_results, sem_hits = _eval_retriever("semantic", skb, GOLD, args.k)
    else:
        sem_results, sem_hits = None, None

    # Per-question breakdown (handy for spotting which questions each retriever
    # gets right vs wrong — those are the prompts to look at for further tuning).
    _print_per_question("keyword KB", kw_results)
    if sem_results is not None:
        _print_per_question("semantic KB", sem_results)

    # Side-by-side summary
    print("\n=== RECALL@{} SUMMARY ===".format(args.k))
    n = len(GOLD)
    print(f"  keyword KB:   {kw_hits}/{n}  ({kw_hits * 100 / n:.1f}%)")
    if sem_hits is not None:
        delta = sem_hits - kw_hits
        sign = "+" if delta >= 0 else ""
        print(f"  semantic KB:  {sem_hits}/{n}  ({sem_hits * 100 / n:.1f}%)   "
              f"[{sign}{delta} vs keyword]")

        # Disagreements — questions where exactly one retriever wins
        sem_only = [(k_r["question"], s_r["top_doc"])
                    for k_r, s_r in zip(kw_results, sem_results)
                    if not k_r["hit"] and s_r["hit"]]
        kw_only = [(k_r["question"], k_r["top_doc"])
                   for k_r, s_r in zip(kw_results, sem_results)
                   if k_r["hit"] and not s_r["hit"]]
        if sem_only:
            print(f"\n  Semantic wins ({len(sem_only)}) — keyword missed these:")
            for q, top in sem_only:
                print(f"    + {q[:80]}")
        if kw_only:
            print(f"\n  Keyword wins ({len(kw_only)}) — semantic missed these:")
            for q, top in kw_only:
                print(f"    + {q[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
