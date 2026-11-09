from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from acadadviser.billing import interpret
from acadadviser.db import Database, LimitReached, PaymentRequired, User
from conftest import (
    another_user, fresh_database, make_client, send_event, sign_in, start, subscribe, subscription_event,
)


def account(client):
    return client.get("/api/account").json()


# Signing in

def test_signed_out_visitors_are_asked_to_sign_in(tmp_path, psa):
    client = make_client(tmp_path, psa, user=None)
    assert client.get("/api/account").status_code == 401
    assert start(client).status_code == 401
    assert client.post("/api/billing/checkout", json={"renewal_consent": True}).status_code == 401
    assert client.post("/api/billing/portal").status_code == 401
    assert psa.creates == []


def test_bad_token_is_rejected(tmp_path, psa):
    client = make_client(tmp_path, psa, user=None)
    assert client.post("/api/session", json={"id_token": "forged"}).status_code == 401


def test_sign_out_ends_the_session(client):
    assert client.post("/api/session/sign-out").status_code == 200
    assert client.get("/api/account").status_code == 401


# The free search

def test_one_free_search_then_payment_is_required(client, psa):
    assert account(client)["free_search_available"] is True
    assert start(client).json()["plan"] == "free"
    response = start(client)
    assert response.status_code == 402
    assert response.json()["subscribe"] == "/account"
    assert len(psa.creates) == 1
    assert account(client)["free_search_available"] is False


def test_failed_free_search_is_given_back(client, psa):
    psa.create_status = 500
    assert start(client).status_code == 502
    psa.create_status = 200
    assert start(client).status_code == 200


def test_email_aliases_share_one_free_search(client):
    assert start(client).status_code == 200
    alias = another_user(client, "user-9", "alice+again@example.com")
    assert start(alias).status_code == 402


def test_free_search_refinements_stop_at_the_free_limit(tmp_path, psa):
    client = make_client(tmp_path, psa, free_follow_ups=1)
    search = start(client).json()["search_id"]
    assert client.post(f"/api/searches/{search}/find-more").status_code == 200
    assert client.post(f"/api/searches/{search}/find-more").status_code == 402
    subscribe(client)
    assert client.post(f"/api/searches/{search}/find-more").status_code == 200


def test_two_free_searches_at_once_only_one_wins(tmp_path):
    database = Database(fresh_database(tmp_path))
    user = User("user-1", "alice@example.com", None)

    def attempt(_):
        try:
            return database.reserve_search(
                user, "alice@example.com", "1.2.3.4", global_limit=100, ip_limit=100, user_limit=100
            )
        except PaymentRequired:
            return None

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert results.count("free") == 1


# Subscriptions

def test_subscriber_searches_past_the_free_search_up_to_fair_use(tmp_path, psa):
    client = make_client(tmp_path, psa, searches_per_user_per_day=3)  # The free search counts too.
    assert start(client).status_code == 200
    subscribe(client)
    assert account(client)["subscribed"] is True
    assert start(client).json()["plan"] == "paid"
    assert start(client).json()["plan"] == "paid"
    response = start(client)
    assert response.status_code == 429
    assert "fair-use" in response.json()["detail"]


def test_cancelled_subscription_stops_new_searches_and_paid_refinements(client):
    subscribe(client)
    search = start(client).json()["search_id"]
    subscribe(client, status="canceled", kind="deleted")
    assert account(client)["subscribed"] is False
    assert client.post(f"/api/searches/{search}/find-more").status_code == 402
    assert start(client).status_code == 200  # The free search was never used.
    assert start(client).status_code == 402
    assert client.get(f"/api/searches/{search}").status_code == 200


def test_past_due_keeps_access_while_stripe_retries(client):
    subscribe(client, status="past_due")
    assert account(client)["subscribed"] is True


def test_cancel_at_period_end_is_reported(client):
    subscribe(client, cancelling=True, period_end=1_900_000_000)
    body = account(client)
    assert body["subscribed"] is True
    assert body["cancel_at_period_end"] is True
    assert body["current_period_end"] == 1_900_000_000


def test_portal_cancellation_by_date_is_reported(client):
    event = subscription_event(period_end=1_900_000_000)
    event["data"]["object"]["cancel_at"] = 1_900_000_000
    assert send_event(client, event).status_code == 200
    body = account(client)
    assert body["subscribed"] is True
    assert body["cancel_at_period_end"] is True
    assert body["current_period_end"] == 1_900_000_000


def test_older_event_does_not_overwrite_a_newer_one(client):
    now = int(time.time())
    subscribe(client, status="active", created=now)
    subscribe(client, status="incomplete", created=now - 60)
    assert account(client)["status"] == "active"


def test_old_subscription_ending_does_not_cancel_its_replacement(client):
    subscribe(client, sub_id="sub_new", status="active")
    subscribe(client, sub_id="sub_old", status="canceled", kind="deleted")
    assert account(client)["subscribed"] is True


def test_subscription_without_metadata_matches_by_customer(client):
    checkout = {
        "id": "evt_checkout", "type": "checkout.session.completed", "created": int(time.time()),
        "data": {"object": {"mode": "subscription", "client_reference_id": "user-1",
                            "customer": "cus_9", "customer_details": {"address": {"country": "US"}}}},
    }
    assert send_event(client, checkout).status_code == 200
    subscribe(client, user=None, customer="cus_9")
    assert account(client)["subscribed"] is True
    assert account(client)["has_billing_account"] is True


# Webhooks

def test_webhook_rejects_a_bad_signature(client):
    response = send_event(client, subscription_event(), secret="whsec_wrong")
    assert response.status_code == 400
    assert account(client)["subscribed"] is False


def test_replayed_event_is_applied_once(client):
    event = subscription_event(status="active")
    assert send_event(client, event).status_code == 200
    subscribe(client, status="canceled", kind="deleted")
    assert send_event(client, event).status_code == 200
    assert account(client)["subscribed"] is False


def test_period_end_is_read_from_the_subscription_or_its_items():
    event = subscription_event(period_end=1_800_000_000)
    assert interpret(event).subscription.current_period_end == 1_800_000_000
    event["data"]["object"]["current_period_end"] = 1_700_000_000
    assert interpret(event).subscription.current_period_end == 1_700_000_000


# Checkout and the billing portal

def test_checkout_needs_renewal_consent(client, billing):
    response = client.post("/api/billing/checkout", json={})
    assert response.status_code == 400
    assert billing.checkouts == []


def test_checkout_starts_a_stripe_session_for_this_user(client, billing):
    response = client.post("/api/billing/checkout", json={"renewal_consent": True})
    assert response.json() == {"url": "https://checkout.stripe.test/session"}
    assert billing.checkouts == [{
        "user_sub": "user-1", "email": "alice@example.com", "customer_id": None,
        "base_url": "http://testserver",
    }]


def test_subscribers_cannot_check_out_twice(client, billing):
    subscribe(client)
    assert client.post("/api/billing/checkout", json={"renewal_consent": True}).status_code == 409
    assert billing.checkouts == []


def test_portal_needs_a_billing_account(client, billing):
    assert client.post("/api/billing/portal").status_code == 404
    subscribe(client, customer="cus_7")
    assert client.post("/api/billing/portal").json() == {"url": "https://billing.stripe.test/portal"}
    assert billing.portals == ["cus_7"]


def test_billing_off_until_configured(tmp_path, psa):
    from acadadviser.billing import Billing

    client = make_client(tmp_path, psa, Billing("", "", ""))
    assert account(client)["billing_available"] is False
    assert client.post("/api/billing/checkout", json={"renewal_consent": True}).status_code == 503
    assert send_event(client, subscription_event()).status_code == 503


def test_session_survives_a_new_sign_in_for_the_same_person(client):
    subscribe(client)
    sign_in(client, "user-1", "alice@example.com")
    assert account(client)["subscribed"] is True


def test_db_limits_raise_the_visitor_messages(tmp_path):
    database = Database(fresh_database(tmp_path))
    user = User("user-1", "alice@example.com", None)
    database.reserve_search(user, "alice@example.com", "ip", global_limit=1, ip_limit=5, user_limit=5)
    database.release_search(user, "free")
    try:
        database.reserve_search(user, "alice@example.com", "ip", global_limit=1, ip_limit=5, user_limit=5)
    except LimitReached as exc:
        assert "capacity" in str(exc)
    else:
        raise AssertionError("global limit not enforced")
