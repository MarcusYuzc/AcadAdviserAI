from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from itertools import count

import httpx
import pytest
from fastapi.testclient import TestClient

from acadadviser.app import create_app
from acadadviser.auth import Identity, InvalidToken
from acadadviser.billing import Billing
from acadadviser.config import Settings

# Unset, tests use SQLite. Set it to a throwaway Postgres to run the suite the way production stores data.
PG_URL = os.environ.get("ACADADVISER_TEST_DATABASE_URL", "")
PDF = b"%PDF-1.7 synthetic"
WEBHOOK_SECRET = "whsec_test"
ITEM = {
    "web_id": "web:1", "university_name": "Example U", "display_name": "MS Statistics",
    "degree_level": "ms", "official_url": "https://stat.example.edu/ms", "why": "Fits.",
    "bucket": "target", "warnings": [], "internal_field": "must not leak",
}
_event_ids = count(1)


class StubCatalog:
    def enrich(self, items):
        return [{**item, "catalog": {"gre_policy": "optional"}} for item in items]


class StubVerifier:
    """Tokens look like "good:<sub>:<email>"; anything else is rejected."""

    def verify(self, token):
        kind, _, rest = token.partition(":")
        sub, _, email = rest.partition(":")
        if kind != "good" or not sub or not email:
            raise InvalidToken("bad")
        return Identity(sub=sub, email=email)


class StubBilling(Billing):
    """Real webhook signature checks; no calls to Stripe."""

    def __init__(self):
        super().__init__("sk_test_stub", WEBHOOK_SECRET, "price_test")
        self.checkouts = []
        self.portals = []

    def checkout_url(self, **kwargs):
        self.checkouts.append(kwargs)
        return "https://checkout.stripe.test/session"

    def portal_url(self, customer_id, base_url):
        self.portals.append(customer_id)
        return "https://billing.stripe.test/portal"


class StubPsa:
    def __init__(self):
        self.creates = []
        self.posts = []
        self.create_status = 200
        self.next_thread = 0
        self.events = [
            {"type": "psa.tool.start", "name": "get_student"},
            {"type": "psa.tool.done", "name": "get_student", "output": "PRIVATE PROFILE"},
            {"type": "response.web_search_call"},
            {"type": "psa.run.ready", "run_id": "run_1", "items": [ITEM]},
            {"type": "agent.session.turn.output_text.done", "text": "Done."},
            {"type": "psa.error", "message": "secret upstream detail"},
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/v1/threads":
            self.creates.append(request.content)
            if self.create_status != 200:
                return httpx.Response(self.create_status, json={"detail": "secret upstream detail"})
            self.next_thread += 1
            return httpx.Response(200, json={"thread_id": f"thr_{self.next_thread}"})
        if path.endswith("/events"):
            body = "".join(f"data: {json.dumps(event)}\n\n" for event in self.events)
            return httpx.Response(200, text=": ping\n\n" + body)
        if path.endswith("/report"):
            return httpx.Response(200, text="<html>report</html>")
        if path.startswith("/v1/runs/"):
            return httpx.Response(200, json={})
        if request.method == "GET":
            return httpx.Response(200, json={
                "latest_run_id": "run_1",
                "student_id": "psa-private",
                "latest_run": {"run_id": "run_1", "items": [ITEM]},
            })
        self.posts.append((path, json.loads(request.content or b"{}")))
        return httpx.Response(200, json={"items": [ITEM]})


@pytest.fixture
def psa():
    return StubPsa()


@pytest.fixture
def billing():
    return StubBilling()


def fresh_database(tmp_path):
    if not PG_URL:
        return tmp_path / "gateway.sqlite3"
    import psycopg

    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    return PG_URL


def make_client(tmp_path, psa, billing=None, *, user=("user-1", "alice@example.com"), **overrides):
    target = fresh_database(tmp_path)
    settings = Settings(**{
        "_env_file": None, "data_dir": tmp_path, "cookie_secure": False, "contact_email": "hello@example.com",
        "site_domain": "example.com", "database_url": PG_URL, **overrides,
    })
    assert settings.database_target() == target
    app = create_app(
        settings, transport=httpx.MockTransport(psa), catalog=StubCatalog(),
        verifier=StubVerifier(), billing=billing or StubBilling(),
    )
    client = TestClient(app)
    if user:
        sign_in(client, *user)
    return client


def sign_in(client, sub, email):
    response = client.post("/api/session", json={"id_token": f"good:{sub}:{email}"})
    assert response.status_code == 200
    return response


def another_user(client, sub="user-2", email="bob@example.com"):
    other = TestClient(client.app)
    sign_in(other, sub, email)
    return other


@pytest.fixture
def client(tmp_path, psa, billing):
    return make_client(tmp_path, psa, billing)


def start(client, **data):
    return client.post(
        "/api/searches",
        data={"consent": "yes", **data},
        files={"resume": ("resume.pdf", PDF, "application/pdf")},
    )


def send_event(client, event, secret=WEBHOOK_SECRET):
    payload = json.dumps(event)
    stamp = int(time.time())
    signature = hmac.new(secret.encode(), f"{stamp}.{payload}".encode(), hashlib.sha256).hexdigest()
    return client.post(
        "/api/stripe/webhook", content=payload,
        headers={"Stripe-Signature": f"t={stamp},v1={signature}", "Content-Type": "application/json"},
    )


def subscription_event(user="user-1", status="active", *, sub_id="sub_1", customer="cus_1",
                       created=None, period_end=1_900_000_000, cancelling=False, kind="updated"):
    return {
        "id": f"evt_{next(_event_ids)}",
        "type": f"customer.subscription.{kind}",
        "created": created or int(time.time()),
        "data": {"object": {
            "id": sub_id, "object": "subscription", "customer": customer, "status": status,
            "cancel_at_period_end": cancelling,
            "metadata": {"user_sub": user} if user else {},
            "items": {"data": [{"current_period_end": period_end}]},
        }},
    }


def subscribe(client, user="user-1", **kwargs):
    assert send_event(client, subscription_event(user, **kwargs)).status_code == 200
