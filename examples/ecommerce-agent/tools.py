"""E-commerce agent tools — search products, look up orders, process refunds.

Pure functions reading from the local JSON files in ``data/``. Each returns a
dict the agent can show to the user (or feed back into the model).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_DATA = _HERE / "data"


def _load(name: str) -> Any:
    return json.loads((_DATA / name).read_text(encoding="utf-8"))


def search_products(query: str, *, limit: int = 3) -> list[dict[str, Any]]:
    """Naive substring + token search over name / description / category / sku."""
    query = (query or "").strip().lower()
    if not query:
        return []
    tokens = [t for t in query.split() if len(t) > 1]
    out: list[tuple[int, dict[str, Any]]] = []
    for p in _load("products.json")["products"]:
        haystack = " ".join([
            p["sku"].lower(), p["name"].lower(),
            p["category"].lower(), p.get("description", "").lower(),
        ])
        if query in haystack:
            score = 10
        else:
            score = sum(1 for t in tokens if t in haystack)
        if score > 0:
            out.append((score, p))
    out.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in out[:limit]]


def get_product(sku: str) -> dict[str, Any] | None:
    sku = (sku or "").strip().upper()
    for p in _load("products.json")["products"]:
        if p["sku"] == sku:
            return p
    return None


def get_order(order_id: str) -> dict[str, Any] | None:
    """Look up an order with its product details enriched."""
    order_id = (order_id or "").strip().upper()
    for o in _load("orders.json")["orders"]:
        if o["order_id"] == order_id:
            product = get_product(o["sku"])
            return {**o, "product": product}
    return None


def get_customer_orders(customer_id: str) -> list[dict[str, Any]]:
    cid = (customer_id or "").strip().upper()
    return [o for o in _load("orders.json")["orders"] if o["customer_id"] == cid]


def get_customer(customer_id: str) -> dict[str, Any] | None:
    cid = (customer_id or "").strip().upper()
    for c in _load("customers.json")["customers"]:
        if c["customer_id"] == cid:
            return c
    return None


def request_refund(order_id: str, reason: str) -> dict[str, Any]:
    """Eligibility check + 'submit' a refund. Returns an outcome dict.

    Rules (so the agent can be wrong about them in fun ways):
      - Only ``delivered`` orders are refundable.
      - Must be within the product's ``return_window_days`` of ``delivered_at``.
      - ``cancelled`` orders aren't refundable (never paid for it).
    """
    from datetime import date, datetime
    order = get_order(order_id)
    if order is None:
        return {"ok": False, "code": "not_found", "message": f"order {order_id!r} not found"}
    if order["status"] == "cancelled":
        return {"ok": False, "code": "cancelled",
                "message": "cancelled orders aren't refundable — payment was never captured"}
    if order["status"] != "delivered":
        return {"ok": False, "code": "not_delivered",
                "message": f"order is still {order['status']} — wait until it's delivered then start a return"}
    if not order.get("delivered_at"):
        return {"ok": False, "code": "no_delivery_date",
                "message": "delivery date is missing; please contact a human agent"}
    window = (order.get("product") or {}).get("return_window_days", 30)
    delivered = datetime.strptime(order["delivered_at"], "%Y-%m-%d").date()
    today = date.today()
    age = (today - delivered).days
    if age > window:
        return {"ok": False, "code": "window_expired",
                "message": f"return window was {window} days; this order was delivered {age} days ago"}
    return {
        "ok": True,
        "code": "approved",
        "refund_id": f"R-{order_id}-A",
        "amount_usd": order["total_usd"],
        "message": f"Refund of ${order['total_usd']:.2f} approved for order {order_id}. "
                   f"Expect 5-7 business days back to your original payment method. "
                   f"Reason logged: {reason}",
    }


# ── Tool catalog the agent advertises to the LLM ────────────────────────────
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "Search the product catalog by query string. Returns matching products.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "e.g. 'headphones', 'wireless charger', 'GPS watch'"},
                    "limit": {"type": "integer", "description": "max results (default 3)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product",
            "description": "Look up one product by SKU.",
            "parameters": {
                "type": "object",
                "properties": {"sku": {"type": "string", "description": "e.g. 'HDPHN-101'"}},
                "required": ["sku"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_order",
            "description": "Look up one order by id. Includes the product, status, tracking, dates.",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string", "description": "e.g. 'A1001'"}},
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_customer_orders",
            "description": "List a customer's orders.",
            "parameters": {
                "type": "object",
                "properties": {"customer_id": {"type": "string", "description": "e.g. 'C-101'"}},
                "required": ["customer_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_refund",
            "description": "Submit a refund for an order. Checks eligibility (delivered + within return window).",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "reason": {"type": "string", "description": "short reason from the customer"},
                },
                "required": ["order_id", "reason"],
            },
        },
    },
]


# ── Dispatch table the agent uses to actually execute a tool call ───────────
TOOL_DISPATCH = {
    "search_products": search_products,
    "get_product": get_product,
    "get_order": get_order,
    "get_customer_orders": get_customer_orders,
    "request_refund": request_refund,
}
