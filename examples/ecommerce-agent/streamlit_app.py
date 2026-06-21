"""ShopMate chat UI — the customer-facing surface of the e-commerce agent.

Streamlit serves a small chat experience; behind the scenes each user turn
runs through :class:`agent.ShopMate`, which talks to ModelArk and posts the
captured Trajectory to aprntc over HTTP.

Run:
  cd examples/ecommerce-agent
  pip install -r requirements.txt
  streamlit run streamlit_app.py
"""

from __future__ import annotations

import os
import sys

import streamlit as st

# Add the parent .venv's aprntc package + this example folder to sys.path
# so we can run `streamlit run` from anywhere.
sys.path.insert(0, os.path.dirname(__file__))

# Best-effort: also load `.env` from the aprntc repo root so ARK_* keys are picked up.
_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_env_file = os.path.join(_repo_root, ".env")
if os.path.exists(_env_file):
    for line in open(_env_file, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from agent import AGENT_ID, ShopMate, _http_json  # noqa: E402


def _vote(aprntc_url: str, episode_id: str, vote: str) -> None:
    """POST a 👍/👎 to aprntc's feedback endpoint and surface the result."""
    try:
        resp = _http_json(
            "POST",
            f"{aprntc_url.rstrip('/')}/api/feedback",
            body={"episode_id": episode_id, "vote": vote},
        )
        st.toast(f"Feedback recorded · fused reward = {resp.get('fused_reward')}", icon="📝")
    except Exception as exc:  # noqa: BLE001
        st.toast(f"Feedback failed: {exc}", icon="⚠️")

st.set_page_config(page_title="ShopMate — Demo Store Support", page_icon="🛍️", layout="centered")


# ── Sidebar: configuration + how-to ─────────────────────────────────────────
with st.sidebar:
    st.markdown("### ShopMate (demo)")
    st.caption("E-commerce customer-support agent · tapped into aprntc")
    st.markdown("---")

    aprntc_url = st.text_input("aprntc URL", value=os.environ.get("APRNTC_URL", "http://localhost:8000"))
    customer_id = st.selectbox(
        "Signed in as",
        ["C-101", "C-102", "C-103", "C-104", "C-105", "(no login)"],
        index=0,
        help="The customer the chat is on behalf of — passed to the agent as a soft hint.",
    )
    model = os.environ.get("APRNTC_POLICY_MODEL", "")
    api_key = os.environ.get("ARK_API_KEY", "")
    base_url = os.environ.get("ARK_BASE_URL", "https://ark.ap-southeast.bytepluses.com/api/v3")

    st.markdown("---")
    if st.button("↻ Refresh playbook from aprntc"):
        if "agent" in st.session_state:
            st.session_state.agent.refresh_playbook()
            st.success("Playbook refreshed.")
    if st.button("🗑️ Reset chat"):
        st.session_state.pop("agent", None)
        st.session_state.pop("history", None)
        st.rerun()

    st.markdown("---")
    st.markdown("**Demo prompts**")
    st.code("Hi! What's the status of order A1001?\n"
            "I need to refund my purchase A1003.\n"
            "Do you have noise-canceling headphones in stock?\n"
            "Where is my order A1002?", language=None)
    st.markdown("---")
    st.caption("Dashboard: " + f"[{aprntc_url}](" + aprntc_url + ")")


# ── Pre-flight: keys present? ───────────────────────────────────────────────
if not api_key or not model:
    st.error(
        "Missing ARK_API_KEY or APRNTC_POLICY_MODEL. Set them in `aprntc/.env` (the demo "
        "auto-loads it) or in your shell before running streamlit."
    )
    st.stop()

# ── Lazy-create the agent once per session ───────────────────────────────────
if "agent" not in st.session_state:
    cid = None if customer_id == "(no login)" else customer_id
    try:
        st.session_state.agent = ShopMate(
            aprntc_url=aprntc_url,
            model=model,
            api_key=api_key,
            base_url=base_url,
            customer_id=cid,
        )
    except Exception as exc:
        st.error(f"Couldn't reach aprntc at {aprntc_url}. Is the server running?\n\n{exc}")
        st.stop()
    st.session_state.history = []
    st.session_state.last_episode_id = None

agent: ShopMate = st.session_state.agent

# ── Main: chat ──────────────────────────────────────────────────────────────
st.title("🛍️ ShopMate")
st.caption(f"Agent ID: `{AGENT_ID}` · Model: `{model}` · Customer: `{customer_id}`")

with st.expander("Active system prompt (fetched from aprntc)", expanded=False):
    st.code(agent.system_prompt or "(empty)", language="text")

for msg in st.session_state.history:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("tool_calls"):
            with st.expander(f"Tools used ({len(msg['tool_calls'])})", expanded=False):
                for tc in msg["tool_calls"]:
                    st.markdown(f"- **{tc['name']}**(`{tc['args']}`)")
                    st.json(tc["result"], expanded=False)
        if msg.get("episode_id"):
            st.caption(f"📥 trajectory `{msg['episode_id']}` posted to aprntc.")

user_text = st.chat_input("Ask about products, orders, or refunds…")
if user_text:
    # Render user message immediately.
    with st.chat_message("user"):
        st.markdown(user_text)
    st.session_state.history.append({"role": "user", "content": user_text})

    # Run the agent.
    with st.chat_message("assistant"):
        with st.spinner("Thinking…"):
            try:
                result = agent.ask(user_text)
            except Exception as exc:
                st.error(f"Agent error: {exc}")
                st.stop()
        st.markdown(result.answer)
        if result.tool_calls:
            with st.expander(f"Tools used ({len(result.tool_calls)})", expanded=False):
                for tc in result.tool_calls:
                    st.markdown(f"- **{tc['name']}**(`{tc['args']}`)")
                    st.json(tc["result"], expanded=False)
        if result.posted_episode_id:
            st.caption(f"📥 trajectory `{result.posted_episode_id}` posted to aprntc.")
        else:
            st.caption("⚠️ trajectory NOT posted (aprntc unreachable?)")

    st.session_state.history.append({
        "role": "assistant",
        "content": result.answer,
        "tool_calls": result.tool_calls,
        "episode_id": result.posted_episode_id,
    })
    st.session_state.last_episode_id = result.posted_episode_id

# ── Footer: a tiny 👍/👎 widget for the last answer (writes EXPLICIT feedback) ──
if st.session_state.get("last_episode_id"):
    st.markdown("---")
    col1, col2, col3 = st.columns([1, 1, 6])
    eid = st.session_state.last_episode_id
    with col1:
        if st.button("👍", key=f"up-{eid}"):
            _vote(aprntc_url, eid, "up")
    with col2:
        if st.button("👎", key=f"down-{eid}"):
            _vote(aprntc_url, eid, "down")
    with col3:
        st.caption(f"Rate the last answer → flows back to aprntc as USER_EXPLICIT feedback.")
