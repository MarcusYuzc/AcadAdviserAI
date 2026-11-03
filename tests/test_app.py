from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from acadadviser.app import create_app
from acadadviser.config import Settings

PDF = b"%PDF-1.7 synthetic"
ITEM = {
    "web_id": "web:1", "university_name": "Example U", "display_name": "MS Statistics",
    "degree_level": "ms", "official_url": "https://stat.example.edu/ms", "why": "Fits.",
    "bucket": "target", "warnings": [], "internal_field": "must not leak",
}


class StubCatalog:
    def enrich(self, items):
        return [{**item, "catalog": {"gre_policy": "optional"}} for item in items]


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


def make_client(tmp_path, psa, **overrides):
    settings = Settings(
        _env_file=None, data_dir=tmp_path, cookie_secure=False, contact_email="hello@example.com",
        site_domain="example.com", **overrides,
    )
    app = create_app(settings, transport=httpx.MockTransport(psa), catalog=StubCatalog())
    return TestClient(app)


@pytest.fixture
def client(tmp_path, psa):
    return make_client(tmp_path, psa)


def start(client, **data):
    return client.post(
        "/api/searches",
        data={"consent": "yes", **data},
        files={"resume": ("resume.pdf", PDF, "application/pdf")},
    )


def test_pages_render_contact_and_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "mailto:hello@example.com" in response.text
    assert "{{" not in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "hello@example.com" in client.get("/privacy").text
    assert client.get("/terms").status_code == 200
    assert client.head("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_form_lives_on_the_advisor_page_not_home(client):
    home = client.get("/").text
    assert "<form" not in home and "<script" not in home
    assert 'href="/signin"' in home
    assert "/advisor" not in home
    advisor = client.get("/advisor").text
    assert 'id="profile-form"' in advisor
    assert "/static/app.js" in advisor
    assert client.get("/signin", follow_redirects=False).headers["location"] == "/advisor"


def test_internal_routes_are_not_exposed(client):
    for path in ("/v1/threads", "/v1/students/x", "/docs", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_search_requires_consent_and_a_profile(client, psa):
    assert client.post("/api/searches", data={"background": "x"}).status_code == 400
    assert client.post("/api/searches", data={"consent": "yes"}).status_code == 400
    assert psa.creates == []


def test_rejects_non_document_upload_without_spending_quota(tmp_path, psa):
    client = make_client(tmp_path, psa, searches_per_ip_per_day=1)
    bad = client.post(
        "/api/searches", data={"consent": "yes"},
        files={"resume": ("resume.pdf", b"<script>", "application/pdf")},
    )
    assert bad.status_code == 400
    assert psa.creates == []
    assert start(client).status_code == 200


def test_rejects_oversized_file(tmp_path, psa):
    client = make_client(tmp_path, psa, max_file_bytes=10)
    assert start(client).status_code == 400
    assert psa.creates == []


def test_create_never_forwards_caller_chosen_ids(client, psa):
    response = client.post(
        "/api/searches",
        data={"consent": "yes", "background": "BS Math", "student_id": "victim", "session_id": "s"},
    )
    assert response.status_code == 200
    assert response.json() == {"search_id": "thr_1"}
    assert b"victim" not in psa.creates[0]
    assert b'name="student_id"' not in psa.creates[0]
    assert b"typed-profile.docx" in psa.creates[0]


def test_second_visitor_cannot_read_or_change_a_search(tmp_path, client, psa):
    search = start(client).json()["search_id"]
    assert client.get(f"/api/searches/{search}").status_code == 200

    other = make_client(tmp_path, psa)
    for call in (
        lambda: other.get(f"/api/searches/{search}"),
        lambda: other.get(f"/api/searches/{search}/events"),
        lambda: other.get(f"/api/searches/{search}/report"),
        lambda: other.post(f"/api/searches/{search}/messages", json={"text": "hi"}),
        lambda: other.post(f"/api/searches/{search}/find-more"),
        lambda: other.post(f"/api/searches/{search}/drop", json={"web_id": "web:1"}),
    ):
        assert call().status_code == 404
    assert psa.posts == []


def test_search_payload_hides_internal_fields_and_adds_catalog(client):
    search = start(client).json()["search_id"]
    body = client.get(f"/api/searches/{search}").json()
    assert "student_id" not in json.dumps(body)
    assert "internal_field" not in body["items"][0]
    assert body["items"][0]["catalog"] == {"gre_policy": "optional"}


def test_events_are_allowlisted_and_failures_are_generic(client):
    search = start(client).json()["search_id"]
    text = client.get(f"/api/searches/{search}/events").text
    assert "PRIVATE PROFILE" not in text
    assert "secret upstream detail" not in text
    events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
    assert [event["type"] for event in events] == ["stage", "stage", "list", "turn_done", "failed"]
    assert events[2]["items"][0]["catalog"] == {"gre_policy": "optional"}
    assert "internal_field" not in events[2]["items"][0]


def test_upstream_errors_do_not_leak_detail(tmp_path, psa):
    client = make_client(tmp_path, psa)
    psa.create_status = 500
    response = start(client)
    assert response.status_code == 502
    assert "secret" not in response.text


def test_ip_daily_limit(tmp_path, psa):
    client = make_client(tmp_path, psa, searches_per_ip_per_day=2)
    assert start(client).status_code == 200
    assert start(client).status_code == 200
    assert start(client).status_code == 429
    assert len(psa.creates) == 2


def test_global_limit_counts_follow_ups(tmp_path, psa):
    client = make_client(tmp_path, psa, model_runs_per_day=2)
    search = start(client).json()["search_id"]
    assert client.post(f"/api/searches/{search}/find-more").status_code == 200
    assert client.post(f"/api/searches/{search}/find-more").status_code == 429
    assert start(client).status_code == 429


def test_follow_up_limit_per_search(tmp_path, psa):
    client = make_client(tmp_path, psa, follow_ups_per_search=1)
    search = start(client).json()["search_id"]
    assert client.post(f"/api/searches/{search}/messages", json={"text": "more"}).status_code == 200
    assert client.post(f"/api/searches/{search}/messages", json={"text": "more"}).status_code == 429


def test_drop_uses_the_web_id_and_does_not_spend_quota(tmp_path, psa):
    client = make_client(tmp_path, psa, model_runs_per_day=1)
    search = start(client).json()["search_id"]
    assert client.post(f"/api/searches/{search}/drop", json={"web_id": "web:1"}).status_code == 200
    path, body = psa.posts[0]
    assert path.endswith("/reactions")
    assert body == {"reactions": [{"kind": "exclude_program", "program_id": "web:1"}]}


def test_report_downloads_only_the_latest_owned_run(client):
    search = start(client).json()["search_id"]
    response = client.get(f"/api/searches/{search}/report")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]


def test_cross_site_post_is_rejected(client, psa):
    response = client.post(
        "/api/searches", data={"consent": "yes", "background": "x"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert psa.creates == []


def test_site_origin_is_accepted(client):
    response = client.post(
        "/api/searches", data={"consent": "yes", "background": "x"},
        headers={"Origin": "https://www.example.com"},
    )
    assert response.status_code == 200


def test_session_cookie_is_http_only(client):
    assert "httponly" in start(client).headers["set-cookie"].lower()
