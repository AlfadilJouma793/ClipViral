"""Stripe subscriptions: Checkout to subscribe, Customer Portal to manage or
cancel, and a webhook that keeps each workspace's plan in sync.

Environment:
  STRIPE_SECRET_KEY          sk_live_... / sk_test_...
  STRIPE_WEBHOOK_SECRET      whsec_... (from the webhook endpoint in the Stripe dashboard)
  STRIPE_PRICE_STARTER       price_... (£10/month recurring price)
  STRIPE_PRICE_CREATOR       price_... (£15/month)
  STRIPE_PRICE_PRO           price_... (£29/month)
  CLIPVIRAL_PUBLIC_URL       https://your-domain (used for redirect URLs)
"""
from __future__ import annotations

import logging
import os
import time

from . import accounts

log = logging.getLogger("clipviral.billing")
PAID = ("starter", "creator", "pro")


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def price_ids() -> dict[str, str]:
    return {p: _env(f"STRIPE_PRICE_{p.upper()}") for p in PAID}


def enabled() -> bool:
    return bool(_env("STRIPE_SECRET_KEY") and all(price_ids().values()))


def public_url(fallback: str) -> str:
    return (_env("CLIPVIRAL_PUBLIC_URL") or fallback).rstrip("/")


def _stripe():
    import stripe
    stripe.api_key = _env("STRIPE_SECRET_KEY")
    return stripe


def plan_for_price(price_id: str | None) -> str | None:
    for plan, pid in price_ids().items():
        if pid and pid == price_id:
            return plan
    return None


def checkout_url(ws: dict, user: dict, plan: str, base_url: str) -> str:
    if plan not in PAID:
        raise ValueError("Unknown plan")
    if not enabled():
        raise RuntimeError("Upgrades aren't available yet.")
    stripe = _stripe()
    base = public_url(base_url)
    params = {
        "mode": "subscription",
        "line_items": [{"price": price_ids()[plan], "quantity": 1}],
        "success_url": f"{base}/studio?billing=success",
        "cancel_url": f"{base}/studio?billing=cancelled",
        "client_reference_id": ws["id"],
        "metadata": {"workspace_id": ws["id"], "plan": plan},
        "subscription_data": {"metadata": {"workspace_id": ws["id"], "plan": plan}},
        "allow_promotion_codes": True,
    }
    if ws.get("stripe_customer_id"):
        params["customer"] = ws["stripe_customer_id"]
    elif user and user.get("email"):
        params["customer_email"] = user["email"]
    session = stripe.checkout.Session.create(**params)
    return session.url


def portal_url(ws: dict, base_url: str) -> str:
    if not enabled():
        raise RuntimeError("Upgrades aren't available yet.")
    if not ws.get("stripe_customer_id"):
        raise RuntimeError("This workspace has no subscription yet.")
    stripe = _stripe()
    session = stripe.billing_portal.Session.create(customer=ws["stripe_customer_id"],
                                                   return_url=f"{public_url(base_url)}/studio")
    return session.url


def _workspace_for(obj: dict) -> str | None:
    meta = obj.get("metadata") or {}
    if meta.get("workspace_id") and accounts.get_workspace(meta["workspace_id"]):
        return meta["workspace_id"]
    customer = obj.get("customer")
    if customer:
        with accounts.db() as con:
            r = con.execute("SELECT id FROM workspaces WHERE stripe_customer_id=?", (customer,)).fetchone()
        if r:
            return r["id"]
    return None


def handle_webhook(payload: bytes, sig_header: str | None) -> str:
    """Verify and apply a Stripe event. Returns the event type (for logging)."""
    secret = _env("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET is not set")
    stripe = _stripe()
    event = stripe.Webhook.construct_event(payload, sig_header, secret).to_dict()  # raises on bad signature
    etype = event["type"]
    with accounts.db() as con:  # idempotency: Stripe may deliver an event more than once
        if con.execute("SELECT 1 FROM stripe_events WHERE id=?", (event["id"],)).fetchone():
            return etype
        con.execute("INSERT INTO stripe_events (id, created_at) VALUES (?, ?)", (event["id"], time.time()))
    obj = event["data"]["object"]

    if etype == "checkout.session.completed" and obj.get("mode") == "subscription":
        ws_id = obj.get("client_reference_id") or _workspace_for(obj)
        plan = (obj.get("metadata") or {}).get("plan")
        if ws_id and plan in PAID and accounts.get_workspace(ws_id):
            accounts.set_plan(ws_id, plan, "active", customer=obj.get("customer"),
                              subscription=obj.get("subscription"))
    elif etype in ("customer.subscription.created", "customer.subscription.updated"):
        ws_id = _workspace_for(obj)
        items = (obj.get("items") or {}).get("data") or []
        plan = plan_for_price(((items[0].get("price") or {}).get("id")) if items else None)
        status = obj.get("status")
        if ws_id:
            if status in ("active", "trialing") and plan:
                accounts.set_plan(ws_id, plan, status, customer=obj.get("customer"), subscription=obj.get("id"))
            elif status in ("past_due",) and plan:
                accounts.set_plan(ws_id, plan, status)  # keep access during Stripe's retry period
            elif status in ("canceled", "unpaid", "incomplete_expired"):
                accounts.set_plan(ws_id, "free", status)
    elif etype == "customer.subscription.deleted":
        ws_id = _workspace_for(obj)
        if ws_id:
            accounts.set_plan(ws_id, "free", "canceled")
    return etype
