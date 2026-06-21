"""ShopMate — a small e-commerce customer-support agent wired into aprntc.

This is what a real customer agent looks like in production:
  - Calls an OpenAI-compatible model (BytePlus ModelArk in this demo).
  - Fetches its system prompt at startup from aprntc's playbook registry,
    so promotion in the aprntc dashboard updates the agent's behaviour
    without a redeploy. Falls back to its initial G0 prompt if aprntc
    isn't reachable (fail-open).
  - Records each conversation as a Trajectory and POSTs it to aprntc's
    `/api/trajectories` ingest endpoint. The agent only knows HTTP — no
    aprntc DB credentials needed.

No imports from the ``aprntc`` package except :mod:`aprntc.trajectory` to
build the Episode payload. Everything else is plain HTTP / OpenAI SDK.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from aprntc.trajectory import (  # only used to build the Episode payload
    Collector,
    ContentPart,
    Episode,
    Step,
    StepType,
    Turn,
)

from tools import TOOL_DISPATCH, TOOL_SPECS


AGENT_ID = "ecommerce-support"

# The G0 system prompt registered with aprntc on first run. INTENTIONALLY
# thin — terse, no citation discipline, doesn't explicitly tell the agent
# to verify order ids or check return windows. The kind of prompt a busy
# customer would write on day 1 of going live. aprntc's job is to find the
# rough edges (failure-patterns) and distill a better G1.
G0_SYSTEM_PROMPT = (
    "You are ShopMate, the customer-support agent for an online electronics store. "
    "Help customers with product questions, order status, and refunds. Use the tools "
    "available; if the customer gives you an order id or SKU, look it up. Be concise."
)


def _http_json(method: str, url: str, *, body: dict | None = None, timeout: float = 10.0) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_or_register_playbook(aprntc_url: str) -> str:
    """Get the current active rendered prompt from aprntc; register G0 if missing."""
    try:
        active = _http_json("GET", f"{aprntc_url}/api/playbooks/{AGENT_ID}/active")
        return active["rendered_prompt"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    # First run — register G0.
    body = {"system_prompt": G0_SYSTEM_PROMPT, "directives": [], "exemplars": [], "watch_out": []}
    pb = _http_json("POST", f"{aprntc_url}/api/playbooks/{AGENT_ID}/register", body=body)
    return pb["rendered_prompt"]


def post_trajectory(aprntc_url: str, episode: Episode) -> str | None:
    """Send one Episode to aprntc's ingest endpoint. Best-effort (fail-open)."""
    try:
        resp = _http_json("POST", f"{aprntc_url}/api/trajectories",
                          body=episode.to_dict(), timeout=15.0)
        return resp.get("episode_id")
    except Exception as exc:  # noqa: BLE001 - aprntc outage must not break the agent
        print(f"[shopmate] WARN: failed to post trajectory to aprntc: {exc}")
        return None


@dataclass
class AgentResult:
    """What one user turn produces — the answer + the trajectory we recorded."""
    answer: str
    episode: Episode
    posted_episode_id: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class ShopMate:
    """Tool-using chat agent for the e-commerce store.

    One instance per chat session. The instance caches the active playbook
    (system prompt) — if aprntc promotes a new generation, restart the agent
    or call ``refresh_playbook()`` to pick it up.
    """

    def __init__(
        self,
        *,
        aprntc_url: str,
        model: str,
        api_key: str,
        base_url: str,
        customer_id: str | None = None,
    ) -> None:
        self.aprntc_url = aprntc_url.rstrip("/")
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.customer_id = customer_id
        self.history: list[dict[str, Any]] = []   # chat history across turns
        self.system_prompt = fetch_or_register_playbook(self.aprntc_url)

    def refresh_playbook(self) -> None:
        """Pull the latest active playbook from aprntc (call after a promotion)."""
        self.system_prompt = fetch_or_register_playbook(self.aprntc_url)

    def ask(self, user_text: str) -> AgentResult:
        """Run one user turn. Drives the tool loop until the model produces a final text.

        Builds the canonical Episode along the way, posts it to aprntc, returns the
        answer to the caller (the chat UI).
        """
        turn_index = len([m for m in self.history if m.get("role") == "user"])
        turn = Turn(turn_index=turn_index)
        turn.user_content.append(ContentPart.text_part(user_text))
        step_index = 0
        tool_calls: list[dict[str, Any]] = []

        # Build the message list for this call: system prompt + prior history + new user msg.
        # Add the optional customer-id as a soft hint the model can use across the conversation.
        sys = self.system_prompt
        if self.customer_id:
            sys = f"{sys}\n\nThe signed-in customer's id is {self.customer_id}."
        messages: list[dict[str, Any]] = [{"role": "system", "content": sys}]
        messages.extend(self.history)
        messages.append({"role": "user", "content": user_text})

        ep_start = time.time()
        answer = ""
        max_tool_rounds = 4

        for _ in range(max_tool_rounds + 1):
            call_start = time.perf_counter()
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=TOOL_SPECS,
                tool_choice="auto",
                max_tokens=600,
                temperature=0.3,
            )
            call_ms = (time.perf_counter() - call_start) * 1000

            choice = resp.choices[0]
            msg = choice.message

            # Record the model_call step (tokens + raw assistant message).
            usage = getattr(resp, "usage", None)
            turn.steps.append(Step(
                step_index=step_index,
                type=StepType.MODEL_CALL,
                duration_ms=call_ms,
                tokens=(usage.completion_tokens if usage else None),
            ))
            step_index += 1

            # If the model called tools, execute them and loop with the results.
            if msg.tool_calls:
                # The assistant message that issued the tool calls must stay in
                # the message list so subsequent tool-result messages link properly.
                messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in msg.tool_calls
                    ],
                })
                for tc in msg.tool_calls:
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    fn = TOOL_DISPATCH.get(name)
                    tool_start = time.perf_counter()
                    if fn is None:
                        result = {"error": f"unknown tool {name!r}"}
                    else:
                        try:
                            result = fn(**args)
                        except TypeError as exc:
                            result = {"error": f"bad args for {name}: {exc}"}
                    tool_ms = (time.perf_counter() - tool_start) * 1000

                    turn.steps.append(Step(
                        step_index=step_index,
                        type=StepType.TOOL_CALL,
                        tool_name=name,
                        tool_args=args,
                        tool_result=result,
                        duration_ms=tool_ms,
                    ))
                    step_index += 1
                    tool_calls.append({"name": name, "args": args, "result": result})

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": name,
                        "content": json.dumps(result, ensure_ascii=False),
                    })
                continue  # loop again so the model can synthesize a final answer

            # No more tool calls — the model produced the user-facing answer.
            answer = (msg.content or "").strip()
            break

        else:
            answer = (
                "I tried several lookups but couldn't put together a final answer. "
                "Please rephrase your question or share an order id (e.g. A1001) so I can help."
            )

        turn.agent_content.append(ContentPart.text_part(answer))

        # Build the canonical Episode the trajectory store understands.
        episode = Episode(
            task_input=user_text,
            collector=Collector.EGRESS_PROXY,  # ingested over the HTTP boundary
            final_output=answer,
            turns=[turn],
            generation_id="g0-shopmate",
            model_id=self.model,
            latency_ms=(time.time() - ep_start) * 1000,
        )

        # Update the conversation history for the next turn.
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": answer})

        # POST to aprntc (fail-open: if aprntc is down, the user still gets the answer).
        posted_id = post_trajectory(self.aprntc_url, episode)

        return AgentResult(answer=answer, episode=episode,
                           posted_episode_id=posted_id, tool_calls=tool_calls)
