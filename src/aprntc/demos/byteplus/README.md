# BytePlus support agent (real doc-grounded demo parent)

A production-bound demo parent that answers questions about the BytePlus AI stack
(ModelArk LLM, VikingDB, image/video/speech generation, files API) grounded in the
**actual BytePlus documentation**. Unlike the toy support/RAG demos, this agent has
genuine *headroom* for the apprentice to improve.

## Pieces
- `kb.py` — `build_kb()` chunks markdown docs (default: `/Users/ankur/mdfiles` +
  `.../byteplus-vikingdb-docs`) into retrievable passages; local keyword search
  (TF-IDF + title boost). Fast, dependency-light; a 50% recall@4 baseline on GOLD.
- `kb_semantic.py` — `SemanticKnowledgeBase`: VikingDB-backed semantic retrieval
  using server-side embedding (skylark). Drop-in `search(query, k)` interface —
  the agent code doesn't change to swap. See "Semantic retrieval" below.
- `agent.py` — `ByteplusSupportAgent`: retrieves doc chunks, answers, cites sources.
  - **thin** config (`rich=False`, `retrieve_k=2`, terse prompt): the parent —
    sometimes shallow / uncited / incomplete (real mistakes to learn from).
  - **rich** config (`rich=True`, more chunks, citation discipline): what a good
    child approximates. `agent.as_child(playbook)` makes a child driven by a
    distilled playbook.
- `gold.py` — `GOLD`: 20 hard BytePlus questions + reference facts (the held-out
  promotion-gate ruler; distillation must never train on it).
- `outcome.py` — `byteplus_outcome`: deterministic scorer (grounded + cited + correct).

## Run the loop (live, needs ModelArk keys)
```bash
.venv/bin/python scripts/demo_byteplus_loop.py
```
Thin parent runs the gold questions → distill lessons → rich child → gate (parent vs
child, recused judge). Observed: the child wins ~65–85% — a real, measurable improvement.

## The honest result (see docs/ROADMAP.md A0b)
The apprentice **demonstrably improves a real agent**. It doesn't always clear the
*strict* gate (loss-rate), because a strong base model means both parent and child are
often correct and the judge flips on style. The margin is real but moderate — bigger,
gate-clearing wins need a parent with bigger real flaws or real production traffic.

## Semantic retrieval (optional, recommended for production)

The keyword KB caps out around **50% recall@4** on the GOLD set — it wins on
product-name queries (e.g. "Seedance") but misses on paraphrased ones
("deep reasoning" vs "thinking parameter"). The semantic retriever
(`kb_semantic.SemanticKnowledgeBase`) trades nothing on the former and
generally pulls ahead on the latter.

**One-time provisioning** (programmatic create isn't accepted on this
VikingDB instance — same as the lessons collection). In the BytePlus
VikingDB console, create:

```
collection: ankur_aprntc_byteplus_kb_collection
fields:
  chunk_id   string  PRIMARY KEY
  text       text                          ← the vector (server-side embed)
  doc        string  DEFAULT "unknown"
  section    string  DEFAULT "Overview"
  chunk_ord  int64   DEFAULT 0
index:
  vector:  hnsw + cosine
  scalar:  doc, section
```

**Then index + eval:**

```bash
.venv/bin/python scripts/index_byteplus_kb.py             # upsert all chunks (~1040)
.venv/bin/python scripts/eval_byteplus_retrieval.py       # A/B recall@4 vs keyword
```

The eval prints a side-by-side, lists the disagreements (which retriever each
side wins), and reports the overall delta.

## Toward production
This is the agent intended for eventual production deployment. There, the parent is the
customer's real agent (connected via a tap — see `docs/PRODUCTION.md`), the corpus is the
customer's real docs/tools, and headroom is larger (real domain mistakes, outdated info).
