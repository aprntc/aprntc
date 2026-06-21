"""OTel-collector integration demo — the production wiring example.

A small "customer agent" that instruments its model call with the
OpenTelemetry SDK (OpenLLMetry-style GenAI attributes). The SDK exports
finished spans to an in-process exporter; we feed those spans to
``spans_to_episodes`` (the OTel ingester), and the trajectories land in the
TrajectoryStore — same data path a real OTel Collector would take in
production, just running in one process so the demo is self-contained.

This is the recipe a real OTel-instrumented customer copies:
  external agent (auto-instrumented or hand-instrumented)
   → OpenTelemetry SDK
   → OTLP export
   → small aprntc receiver that calls spans_to_episodes(spans) → sink

Needs:
  - real ModelArk keys in .env (ARK_API_KEY + ARK_BASE_URL + APRNTC_POLICY_MODEL)
  - the [otel] extra: pip install 'aprntc[otel]'

Run:
  .venv/bin/python scripts/demo_otel_agent.py
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# Only aprntc imports a customer would need.
from aprntc.tap import spans_to_episodes
from aprntc.trajectory import Collector, TrajectoryStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="aprntc.db",
                        help="trajectory store SQLite path (default: aprntc.db)")
    parser.add_argument("--question", default="What is OpenTelemetry in one sentence?",
                        help="the user question to ask the external agent")
    args = parser.parse_args()

    try:
        from opentelemetry.sdk.trace import TracerProvider  # noqa: F401
    except ModuleNotFoundError:
        print("ERROR: opentelemetry-sdk not installed. Run: pip install 'aprntc[otel]'",
              file=sys.stderr)
        return 2

    api_key = os.environ.get("ARK_API_KEY")
    base_url = os.environ.get("ARK_BASE_URL", "https://ark.ap-southeast.bytepluses.com/api/v3")
    model = os.environ.get("APRNTC_POLICY_MODEL")
    if not (api_key and model):
        print("ERROR: ARK_API_KEY and APRNTC_POLICY_MODEL must be set in env.",
              file=sys.stderr)
        return 2

    print(f"[1/4] Setting up an OTel tracer with an in-memory exporter …")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("aprntc-external-demo")
    print("  ↳ ready.")

    # The external agent — uses the OpenAI SDK directly (closest to what real
    # customer code looks like). All it adds is a single OTel span around the call.
    print(f"\n[2/4] Calling the model with OTel-instrumented agent code …")
    try:
        from openai import OpenAI
    except ModuleNotFoundError:
        print("ERROR: openai not installed (a transitive dep of [proxy]). Run:"
              " pip install 'aprntc[proxy]'", file=sys.stderr)
        return 2

    client = OpenAI(api_key=api_key, base_url=base_url)
    start = time.time()
    answer = None
    with tracer.start_as_current_span("openai.chat") as span:
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("gen_ai.prompt", args.question)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Answer in one sentence."},
                {"role": "user", "content": args.question},
            ],
            max_tokens=200,
            temperature=0.3,
        )
        answer = resp.choices[0].message.content.strip() if resp.choices else ""
        span.set_attribute("gen_ai.completion", answer)
        if resp.usage:
            span.set_attribute("gen_ai.usage.input_tokens", resp.usage.prompt_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", resp.usage.completion_tokens)
    elapsed = time.time() - start
    print(f"  ↳ answered in {elapsed:.1f}s")
    print(f"  ↳ answer: {answer}")

    # Drain finished spans from the SDK and map them via spans_to_episodes.
    print(f"\n[3/4] Draining the SDK's exporter, ingesting via spans_to_episodes …")
    spans = exporter.get_finished_spans()
    span_dicts = [_to_dict(s) for s in spans]
    episodes = spans_to_episodes(span_dicts)
    print(f"  ↳ {len(spans)} finished spans → {len(episodes)} Episodes")

    # Sink them into the store (the same TrajectoryStore /api/trajectories reads).
    print(f"\n[4/4] Writing Episodes to the trajectory store …")
    store = TrajectoryStore(args.db)
    pre = store.count()
    for ep in episodes:
        store.put_episode(ep, scrub=False)
    post = store.count()
    new_count = post - pre
    print(f"  ↳ episodes added: {new_count}")
    if new_count < 1:
        print("FAIL: no Episode landed in the store.", file=sys.stderr)
        return 1
    latest = store.query(limit=post)[-1]
    if latest.collector is not Collector.OTEL:
        print(f"FAIL: latest collector is {latest.collector.value!r}, expected otel",
              file=sys.stderr)
        return 1

    print()
    print("✓ OTEL AGENT → APRNTC LOOP VERIFIED")
    print(f"  episode_id:    {latest.episode_id}")
    print(f"  collector:     {latest.collector.value}")
    print(f"  model_id:      {latest.model_id}")
    print(f"  task_input:    {(latest.task_input or '')[:60]}…")
    print(f"  final_output:  {(latest.final_output or '')[:60]}…")
    print(f"  tokens in/out: {latest.tokens_in}/{latest.tokens_out}")
    return 0


def _to_dict(readable_span) -> dict:
    """Adapt an OTel ReadableSpan to the dict shape our ingester reads."""
    return {
        "name": readable_span.name,
        "attributes": dict(readable_span.attributes or {}),
        "trace_id": f"{readable_span.context.trace_id:032x}",
    }


if __name__ == "__main__":
    sys.exit(main())
