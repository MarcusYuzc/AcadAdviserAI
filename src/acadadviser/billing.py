"""Stripe Billing for the yearly plan. Card details go to Stripe's pages, never to this server."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import stripe

# past_due keeps access while Stripe retries the card. The dashboard cancels after the last retry.
ACTIVE_STATUSES = frozenset({"active", "trialing", "past_due"})
EVENT_TYPES = (
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
)


class InvalidEvent(Exception):
    pass


@dataclass(frozen=True)
class SubscriptionRecord:
    user_sub: str | None
    customer_id: str
    subscription_id: str
    status: str
    current_period_end: int | None
    cancel_at_period_end: bool


@dataclass(frozen=True)
class StripeUpdate:
    event_id: str
    event_type: str
    created: int
    customer_link: tuple[str, str] | None = None
    subscription: SubscriptionRecord | None = None
    country: str | None = None


def _period_end(subscription: dict[str, Any]) -> int | None:
    # Newer API versions moved the period from the subscription onto its items.
    if subscription.get("current_period_end"):
        return int(subscription["current_period_end"])
    ends = [item.get("current_period_end") for item in (subscription.get("items") or {}).get("data", [])]
    ends = [int(end) for end in ends if end]
    return max(ends) if ends else None


def interpret(event: dict[str, Any]) -> StripeUpdate:
    kind, obj = event["type"], event["data"]["object"]
    base = {"event_id": event["id"], "event_type": kind, "created": int(event["created"])}
    if kind == "checkout.session.completed" and obj.get("mode") == "subscription":
        user_sub, customer = obj.get("client_reference_id"), obj.get("customer")
        address = (obj.get("customer_details") or {}).get("address") or {}
        link = (user_sub, customer) if user_sub and customer else None
        return StripeUpdate(**base, customer_link=link, country=address.get("country"))
    if kind.startswith("customer.subscription."):
        # The billing portal now cancels by setting cancel_at and leaves cancel_at_period_end false.
        cancel_at = obj.get("cancel_at")
        record = SubscriptionRecord(
            user_sub=(obj.get("metadata") or {}).get("user_sub"),
            customer_id=obj["customer"],
            subscription_id=obj["id"],
            status=obj["status"],
            current_period_end=int(cancel_at) if cancel_at else _period_end(obj),
            cancel_at_period_end=bool(obj.get("cancel_at_period_end") or cancel_at),
        )
        return StripeUpdate(**base, subscription=record)
    return StripeUpdate(**base)


class Billing:
    def __init__(self, secret_key: str, webhook_secret: str, price_id: str):
        self.enabled = bool(secret_key and webhook_secret and price_id)
        self._webhook_secret = webhook_secret
        self._price_id = price_id
        self._client = stripe.StripeClient(secret_key) if secret_key else None

    def checkout_url(self, *, user_sub: str, email: str, customer_id: str | None, base_url: str) -> str:
        params: dict[str, Any] = {
            "mode": "subscription",
            "line_items": [{"price": self._price_id, "quantity": 1}],
            "client_reference_id": user_sub,
            "subscription_data": {"metadata": {"user_sub": user_sub}},
            "automatic_tax": {"enabled": True},
            "billing_address_collection": "required",
            "success_url": f"{base_url}/account?checkout=success",
            "cancel_url": f"{base_url}/account",
        }
        if customer_id:
            params["customer"] = customer_id
            params["customer_update"] = {"address": "auto", "name": "auto"}
        else:
            params["customer_email"] = email
        return self._client.v1.checkout.sessions.create(params).url

    def portal_url(self, customer_id: str, base_url: str) -> str:
        params = {"customer": customer_id, "return_url": f"{base_url}/account"}
        return self._client.v1.billing_portal.sessions.create(params).url

    def parse_event(self, payload: bytes, signature: str) -> dict[str, Any]:
        try:
            stripe.WebhookSignature.verify_header(
                payload.decode("utf-8"), signature, self._webhook_secret, tolerance=300
            )
            return json.loads(payload)
        except (stripe.SignatureVerificationError, UnicodeDecodeError, ValueError) as exc:
            raise InvalidEvent(type(exc).__name__) from exc
