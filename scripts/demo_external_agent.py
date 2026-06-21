"""External-agent integration demo — the production wiring example (A1 live verify).

  A small program that doesn't import aprntc internals (simulates a real customer
  agent). It talks to BytePlus ModelArk via the LiteLLM Python SDK with the
  AprntcProxyLogger registered as a callback, fetches its system prompt from
  the aprntc Playbook Registry over HTTP, makes one call, then verifies the
  trajectory appeared in the store via /api/trajectories.

This is the recipe future external customer agents copy from to wire themselves
into aprntc. The egress-proxy collector path (A1) is exercised end-to-end:

  external agent → LiteLLM SDK → AprntcProxyLogger callback
                                 → TrajectoryStore (via sink)

Needs:
  - real ModelArk keys in .env (ARK_API_KEY + ARK_BASE_URL + APRNTC_POLICY_MODEL)
  - a running aprntc server on the same machine (`uvicorn aprntc.web.app:app`)
  - litellm installed: `pip install 'aprntc[proxy]'`

Run:
  .venv/bin/python scripts/demo_external_agent.py
  .venv/bin/python scripts/demo_external_agent.py --aprntc http://localhost:8000

The script exits 0 on full success (model answered + episode captured), prints
the answer + episode summary, and exits non-zero with a clear message otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# Used as the AprntcProxyLogger sink — this is the ONLY aprntc import a real
# external agent needs (the [proxy] extra ships it). Everything else here uses
# the standard libraries an external customer agent would already have.
from aprntc.tap.egress_proxy import make_proxy_logger
from aprntc.trajectory import Collector, TrajectoryStore


AGENT_ID = "external-demo-agent"
G0_SYSTEM_PROMPT = (
    "You are a concise documentation assistant. Answer the user's question in "
    "one or two sentences. If you don't know, say you don't know."
)


def _http_get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_post(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ensure_g0_playbook(server: str) -> str:
    """Register a G0 playbook for this agent if one isn't already there.

    Returns the rendered system prompt the agent will run with (the registry
    serializes ActivePlaybook with `rendered_prompt` already composed from
    the base system prompt + directives + exemplars + watch-outs).
    """
    try:
        active = _http_get(f"{server}/api/playbooks/{AGENT_ID}/active")
        print(f"  ↳ existing playbook (G{active['generation']}, hash={active['hash'][:10]}…)")
        return active["rendered_prompt"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    # No G0 yet — register the initial one.
    body = {"system_prompt": G0_SYSTEM_PROMPT, "directives": [], "exemplars": [], "watch_out": []}
    pb = _http_post(f"{server}/api/playbooks/{AGENT_ID}/register", body)
    print(f"  ↳ registered G0 playbook (hash={pb['hash'][:10]}…)")
    return pb["rendered_prompt"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aprntc", default="http://localhost:8000",
                        help="aprntc server base URL (default: http://localhost:8000)")
    parser.add_argument("--db", default="aprntc.db",
                        help="path to the trajectory store SQLite file (default: aprntc.db)")
    parser.add_argument("--question", default="What is OpenTelemetry in one sentence?",
                        help="the user question to ask the external agent")
    args = parser.parse_args()

    try:
        import litellm  # noqa: F401
    except ModuleNotFoundError:
        print("ERROR: litellm not installed. Run: pip install 'aprntc[proxy]'", file=sys.stderr)
        return 2

    api_key = os.environ.get("ARK_API_KEY")
    base_url = os.environ.get("ARK_BASE_URL", "https://ark.ap-southeast.bytepluses.com/api/v3")
    model = os.environ.get("APRNTC_POLICY_MODEL")
    if not (api_key and model):
        print("ERROR: ARK_API_KEY and APRNTC_POLICY_MODEL must be set in env.", file=sys.stderr)
        return 2

    print(f"[1/4] Aprntc server:  {args.aprntc}")
    print(f"      Agent id:       {AGENT_ID}")
    print(f"      Model:          {model}")
    print()

    # 1) Fetch (or seed) the active playbook from the aprntc Playbook Registry.
    print("[2/4] Fetching active playbook from /api/playbooks/{id}/active …")
    try:
        system_prompt = _ensure_g0_playbook(args.aprntc)
    except urllib.error.URLError as e:
        print(f"  ↳ FAILED — is the aprntc server running? ({e})", file=sys.stderr)
        return 1
    print(f"  ↳ active system prompt: {system_prompt[:80]}…")
    print()

    # 2) Wire the aprntc collector as a LiteLLM callback. ALL model traffic now
    #    flows through the AprntcProxyLogger → TrajectoryStore (collector=egress_proxy).
    print("[3/4] Wiring AprntcProxyLogger as a litellm callback …")
    store = TrajectoryStore(args.db)
    logger = make_proxy_logger(store.put_episode)
    import litellm
    litellm.callbacks = [logger]
    pre_count = store.count()
    print(f"  ↳ ready. Episodes in store before run: {pre_count}")
    print()

    # 3) Make the external-agent call via litellm — this is exactly what a real
    #    customer agent would do; no aprntc-specific code in this block.
    print(f"[4/4] Calling the model:  {args.question!r}")
    start = time.time()
    resp = litellm.completion(
        model=f"openai/{model}",                # litellm provider prefix
        api_key=api_key,
        base_url=base_url,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": args.question},
        ],
        max_tokens=200,
        temperature=0.3,
    )
    answer = resp.choices[0].message.content
    elapsed = time.time() - start
    print(f"  ↳ answered in {elapsed:.1f}s")
    print(f"  ↳ answer: {answer.strip()}")
    print()

    # Give the async callback a beat to flush. (litellm's callback can be sync or
    # async depending on version; we observed it lands synchronously here.)
    time.sleep(0.2)

    # 4) Verify the trajectory landed via /api/trajectories. Critically, with
    #    collector=egress_proxy — proving the LiteLLM → collector → store path works.
    post_count = store.count()
    new_episodes = post_count - pre_count
    print(f"  ↳ episodes added to the store: {new_episodes}")
    if new_episodes < 1:
        print("FAIL: no episode was captured. Is litellm.callbacks set up correctly?", file=sys.stderr)
        return 1

    # Find the newest episode and confirm the collector.
    latest = store.query(limit=1)
    # query() returns oldest-first; take the last (newest) — works even if
    # there are pre-existing episodes from earlier runs.
    latest_all = store.query(limit=post_count)
    ep = latest_all[-1] if latest_all else None
    if ep is None or ep.collector is not Collector.EGRESS_PROXY:
        print(f"FAIL: latest episode collector is {ep.collector if ep else None!r}, "
              f"expected egress_proxy", file=sys.stderr)
        return 1

    print()
    print("✓ EXTERNAL AGENT → APRNTC LOOP VERIFIED")
    print(f"  episode_id:       {ep.episode_id}")
    print(f"  collector:        {ep.collector.value}")
    print(f"  model_id:         {ep.model_id}")
    print(f"  task_input:       {(ep.task_input or '')[:60]}…")
    print(f"  final_output:     {(ep.final_output or '')[:60]}…")
    print(f"  latency_ms:       {ep.latency_ms:.0f}" if ep.latency_ms else "  latency_ms:       (none)")
    print(f"  tokens in/out:    {ep.tokens_in}/{ep.tokens_out}")
    print()
    print(f"View it in the dashboard: {args.aprntc}/trajectories")
    return 0


if __name__ == "__main__":
    sys.exit(main())
