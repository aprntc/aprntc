"""One-time indexer — push the BytePlus KB chunks into VikingDB.

Run AFTER provisioning the collection + index in the BytePlus VikingDB console
(or first via ``--create``, which calls the control plane to do it for you).

  .venv/bin/python scripts/index_byteplus_kb.py            # upsert chunks
  .venv/bin/python scripts/index_byteplus_kb.py --create   # create collection+index FIRST, then upsert
  .venv/bin/python scripts/index_byteplus_kb.py --dry-run  # show counts, don't upload

Idempotent — re-running over the same chunks updates in place (chunk_id is the
primary key). Server-side vectorize caps each upsert at 1 row, so this is a
~chunk_count-call operation; expect a few minutes for ~1000 chunks.
"""

from __future__ import annotations

import argparse
import sys
import time

from aprntc.config import Settings
from aprntc.demos.byteplus.kb import build_kb
from aprntc.demos.byteplus.kb_semantic import SemanticKBError, SemanticKnowledgeBase


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create", action="store_true",
                        help="create the collection + index before upserting (idempotent on errors)")
    parser.add_argument("--dry-run", action="store_true",
                        help="just count the chunks — don't talk to VikingDB at all")
    parser.add_argument("--limit", type=int, default=None,
                        help="only upsert the first N chunks (handy for a sanity test)")
    parser.add_argument("--collection", default=None, help="override the collection name")
    parser.add_argument("--index", default=None, help="override the index name")
    args = parser.parse_args()

    print("[1/3] Building the BytePlus KB from disk …")
    kb = build_kb()
    chunks = kb.chunks if args.limit is None else kb.chunks[: args.limit]
    print(f"  ↳ {len(chunks)} chunks across {len(set(c.doc for c in chunks))} doc areas")
    if args.dry_run:
        print("  ↳ dry-run: stopping here.")
        return 0

    print("[2/3] Loading VikingDB credentials …")
    settings = Settings.from_env(dotenv=".env")
    try:
        settings.vikingdb.validate()
    except Exception as e:
        print(f"  ↳ FAIL: {e}", file=sys.stderr)
        return 2
    kwargs = {}
    if args.collection: kwargs["collection"] = args.collection
    if args.index: kwargs["index"] = args.index
    skb = SemanticKnowledgeBase(settings.vikingdb, **kwargs)
    print(f"  ↳ collection: {skb.collection}")

    if args.create:
        print("[2.5/3] Creating collection + index (idempotent — duplicate-error is OK) …")
        for fn, label in ((skb.create_collection, "collection"), (skb.create_index, "index")):
            try:
                fn()
                print(f"  ↳ created {label}")
            except SemanticKBError as e:
                msg = str(e)
                # Already-exists is fine; surface other errors.
                if "exist" in msg.lower() or "Existed" in msg:
                    print(f"  ↳ {label} already exists — skipping")
                elif "InvalidActionOrVersion" in msg or "Could not find operation" in msg:
                    # Region-specific: programmatic control-plane create isn't accepted
                    # for this VikingDB instance. The user provisions via the console
                    # (same finding as STATUS.md Stage 5 for the lessons collection).
                    print(f"  ↳ programmatic {label} create not supported on this VikingDB instance.")
                    print(f"     → Create it in the BytePlus VikingDB console with this schema:")
                    print(f"        collection: {skb.collection}")
                    print(f"        fields:     chunk_id (string, PK), text (text, vector field),")
                    print(f"                    doc (string), section (string), chunk_ord (int64)")
                    print(f"        index:      hnsw + cosine, scalar indexes on doc, section")
                    print(f"     Then re-run WITHOUT --create.")
                    return 1
                else:
                    print(f"  ↳ {label} create failed: {e}", file=sys.stderr)
                    return 1

    print(f"[3/3] Upserting {len(chunks)} chunks (server-side embed, ~1 req/chunk) …")
    start = time.time()
    written = 0
    for i, c in enumerate(chunks):
        try:
            skb.upsert_chunks([c])
            written += 1
        except SemanticKBError as e:
            print(f"  ↳ chunk {i} ({c.chunk_id}) failed: {e}", file=sys.stderr)
            # keep going — one chunk shouldn't kill the whole job
        if (i + 1) % 50 == 0:
            elapsed = time.time() - start
            rate = (i + 1) / elapsed
            eta = (len(chunks) - (i + 1)) / rate if rate > 0 else 0
            print(f"  ↳ {i + 1}/{len(chunks)} ({rate:.1f}/s, ~{eta:.0f}s remaining)")

    elapsed = time.time() - start
    print(f"\n✓ INDEXED — {written}/{len(chunks)} chunks in {elapsed:.0f}s "
          f"({written / elapsed:.1f}/s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
