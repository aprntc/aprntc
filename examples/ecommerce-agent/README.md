# ShopMate — e-commerce customer-support agent (external to aprntc)

A small reference customer agent showing the full **external-agent + aprntc**
integration on one laptop:

```
              ┌───────────────────┐                ┌─────────────────────┐
   shopper →  │ ShopMate (this)   │  HTTP traffic  │ aprntc server       │
              │ Streamlit chat UI │ ─────────────► │ (FastAPI + dashboard)│
              │ + OpenAI client   │ ◄─────────────│                     │
              │ + 3 tools         │  active prompt │ trajectory store    │
              └───────────────────┘                │ memory · gate · UI  │
                                                   └─────────────────────┘
```

**What it shows:**
- The agent has **no aprntc credentials** beyond an optional API key — it
  reaches aprntc only over HTTP.
- Two simple HTTP integrations cover the entire production loop:
  1. **Fetch the active system prompt** at startup via
     `GET /api/playbooks/ecommerce-support/active`.
  2. **POST each conversation Trajectory** via `POST /api/trajectories`.
- ShopMate's behaviour can change without redeploying the agent — promotion
  in the aprntc dashboard flips what the playbook endpoint returns.

This is the **production-recommended path** for any external customer
agent. The cousin scripts in `aprntc/scripts/demo_*.py` show the same
loop via the LiteLLM, OTel, and MCP collector paths.

---

## Run it

### Prereqs
- Python venv (this repo's `.venv` works fine).
- A running aprntc server (see `docs/DEPLOY.md` — `docker compose up` or
  `uvicorn aprntc.web.app:app --port 8000`).
- BytePlus ModelArk keys in `aprntc/.env` (the demo auto-loads them):
  `ARK_API_KEY`, `APRNTC_POLICY_MODEL`, `ARK_BASE_URL`.

### Install ShopMate's deps + run
```bash
# From the repo root:
.venv/bin/pip install -r examples/ecommerce-agent/requirements.txt
.venv/bin/streamlit run examples/ecommerce-agent/streamlit_app.py
# → http://localhost:8501
```

The agent will auto-register its initial **G0** system prompt with aprntc
on first run (you'll see the agent appear in the dashboard's Trajectories
view as soon as the first message lands).

---

## Try it (suggested customer scenarios)

The dummy data covers 5 products and 6 orders. Open `data/products.json`
and `data/orders.json` to see the catalog.

| Scenario | Try | What's interesting |
|---|---|---|
| Order-status (happy) | `What's the status of order A1001?` | Uses `get_order` → tracking + delivery date. |
| Order-status (in transit) | `Where is my order A1002?` | Uses `get_order` → in-transit + carrier + tracking #. |
| Refund (eligible) | `I need to refund A1003.` | Uses `request_refund` → `delivered` + within window → approved. |
| Refund (not yet delivered) | `Refund my order A1004 please` | `processing` → tool rejects. The agent's thin G0 prompt may forget to explain WHY clearly — a real "watch-out" the apprentice can learn from. |
| Refund (cancelled) | `Refund my order A1006.` | `cancelled` → tool returns "payment never captured". |
| Product question (in stock) | `Do you have noise-canceling headphones?` | Uses `search_products` → AuraSound. |
| Product question (out of stock) | `Are the Pulse Fit smartwatches available?` | `in_stock: false` — does the thin G0 prompt remember to surface that clearly? |
| Multi-tool | `What's the status of A1004 and is the keyboard in stock?` | Multi-step tool plan in one turn. |

After each response click 👍 / 👎 in the chat panel — those flow back to
aprntc as `USER_EXPLICIT` labels (outranking the judge in reward fusion,
per ADR 0006).

---

## What lands in aprntc

Open the dashboard (default `http://localhost:8000`) → **Trajectories**:
- One row per conversation turn, **collector badge = `egress_proxy`** (the
  HTTP boundary), reward shaded by your 👍/👎 (none yet → no reward badge).
- Expand a row → see the full tool-call trace (`search_products`,
  `get_order`, `request_refund` with args/results) and the model's final
  answer. 👍/👎 also available from the dashboard.

---

## The G0 system prompt (intentionally thin)

```text
You are ShopMate, the customer-support agent for an online electronics store.
Help customers with product questions, order status, and refunds. Use the
tools available; if the customer gives you an order id or SKU, look it up.
Be concise.
```

That's the **parent**. Notice what it doesn't say:
- *Verify the order belongs to the signed-in customer before sharing details.*
- *When a refund is rejected, explain WHY in plain English (window, status, etc.).*
- *Mention the warranty period for the product you're discussing.*
- *Suggest alternatives when something is out of stock.*

This is exactly the kind of agent aprntc is designed to improve: the prompt
is sensible enough for day-1 production but has obvious failure-patterns and
unmentioned heuristics — the distillation loop should mine those into a
better G1.

---

## What's NOT included (intentional scope cuts)

- **No real payment / inventory mutation** — `request_refund` returns a
  decision dict but doesn't talk to a payment processor.
- **No persistent customer auth** — the sidebar `customer_id` is a soft
  hint passed to the system prompt; no real session.
- **No streaming** — single-shot completions per turn so the trajectory
  is straightforward to build.

These are normal product details that would matter in a real deployment
but aren't on the path to demonstrating aprntc's integration story.

---

## Files
```
examples/ecommerce-agent/
  data/
    products.json         5 products (sku, price, stock, return-window)
    orders.json           6 orders (delivered / in-transit / processing / cancelled)
    customers.json        5 customers
  tools.py                Tool implementations + the spec list the LLM sees
  agent.py                ShopMate class — playbook fetch, model loop, trajectory POST
  streamlit_app.py        Chat UI (talks to ShopMate, renders the trace, posts thumbs)
  requirements.txt        streamlit + openai (no aprntc deps beyond schema imports)
  README.md               you are here
```

See `docs/EXAMPLE_DEPLOYMENT.md` for the full production-flavoured
walkthrough (provision aprntc → register playbook → run ShopMate →
distill → review → promote → ShopMate picks up the new prompt).
