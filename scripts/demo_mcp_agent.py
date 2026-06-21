"""MCP collector integration demo — the production wiring example.

A self-contained customer-agent scenario:
  * a tiny FastMCP server with one tool (``kb_lookup``)
  * an "external agent" that calls the tool via the MCP SDK
  * each call+result is captured as a gateway-style record (this is what a
    real OSS MCP gateway like IBM ContextForge would log)
  * the records flow through ``mcp_records_to_episode`` into the
    TrajectoryStore with ``collector=mcp``

In production the gateway is the seam; here we run the call in-process so the
demo is self-contained. The mapping logic is identical — what a real
gateway logs (tool name, args, result, timing) is what we ingest.

Needs:
  - the `[mcp]` extra: pip install 'aprntc[mcp]'  (mcp Python SDK)

Run:
  .venv/bin/python scripts/demo_mcp_agent.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from aprntc.tap import mcp_records_to_episode
from aprntc.trajectory import Collector, TrajectoryStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="aprntc.db",
                        help="trajectory store SQLite path (default: aprntc.db)")
    parser.add_argument("--query", default="refund policy",
                        help="the KB query the external agent asks")
    args = parser.parse_args()

    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError:
        print("ERROR: mcp SDK not installed. Run: pip install mcp", file=sys.stderr)
        return 2

    print("[1/4] Standing up a tiny FastMCP server with a kb_lookup tool …")
    server = FastMCP("aprntc-demo")

    @server.tool()
    def kb_lookup(query: str) -> str:
        """Toy KB: returns a canned answer per query (this is the tool the agent calls)."""
        kb = {
            "refund policy": "Refunds within 30 days of purchase with a receipt.",
            "cancel order": "Cancel via account settings; orders ship within 24h after which cancellation isn't possible.",
        }
        return kb.get(query.lower(), f"no entry for {query!r}")
    print(f"  ↳ server: {server.name}")

    # Drive a real MCP tool call. In production this happens via the gateway; the
    # gateway logs the same (name, args, result, timing) record we synthesize here.
    print(f"\n[2/4] External agent calls kb_lookup(query={args.query!r}) …")
    start = time.time()
    raw_result = asyncio.run(server.call_tool("kb_lookup", {"query": args.query}))
    elapsed_ms = (time.time() - start) * 1000
    result_text = _result_text(raw_result)
    print(f"  ↳ tool returned in {elapsed_ms:.1f}ms")
    print(f"  ↳ result: {result_text}")

    # The gateway record — same shape a real ContextForge gateway would log.
    record = {
        "tool.name": "kb_lookup",
        "tool.arguments": {"query": args.query},
        "tool.result": result_text,
        "duration_ms": elapsed_ms,
    }

    print("\n[3/4] Ingesting the gateway record via mcp_records_to_episode …")
    episode = mcp_records_to_episode([record], task_input=args.query)
    if episode is None:
        print("FAIL: ingester didn't produce an Episode.", file=sys.stderr)
        return 1
    print(f"  ↳ Episode: collector={episode.collector.value}, "
          f"{sum(len(t.steps) for t in episode.turns)} tool steps")

    print("\n[4/4] Writing the Episode to the trajectory store …")
    store = TrajectoryStore(args.db)
    pre = store.count()
    eid = store.put_episode(episode, scrub=False)
    post = store.count()
    new_count = post - pre
    print(f"  ↳ episodes added: {new_count}")
    if new_count < 1:
        print("FAIL: no Episode landed in the store.", file=sys.stderr)
        return 1

    latest = store.get_episode(eid)
    if latest.collector is not Collector.MCP:
        print(f"FAIL: latest collector is {latest.collector.value!r}, expected mcp",
              file=sys.stderr)
        return 1

    print()
    print("✓ MCP AGENT → APRNTC LOOP VERIFIED")
    print(f"  episode_id:    {latest.episode_id}")
    print(f"  collector:     {latest.collector.value}")
    step = latest.turns[0].steps[0]
    print(f"  tool_name:     {step.tool_name}")
    print(f"  tool_args:     {step.tool_args}")
    print(f"  tool_result:   {str(step.tool_result)[:60]}…")
    print(f"  duration_ms:   {step.duration_ms:.1f}" if step.duration_ms else "  duration_ms:   (none)")
    return 0


def _result_text(result) -> str:
    """Extract the text payload from a FastMCP call_tool return (tolerant of shape)."""
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, list) and result:
        first = result[0]
        return getattr(first, "text", None) or str(first)
    return str(result)


if __name__ == "__main__":
    sys.exit(main())
