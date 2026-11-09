from __future__ import annotations

import json

from conftest import another_user, make_client, start, subscribe


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
    assert 'id="view-signin"' in client.get("/signin").text
    assert client.get("/signin.html", follow_redirects=False).headers["location"] == "/signin"


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
    assert response.json() == {"search_id": "thr_1", "plan": "free"}
    assert b"victim" not in psa.creates[0]
    assert b'name="student_id"' not in psa.creates[0]
    assert b"typed-profile.docx" in psa.creates[0]


def test_second_visitor_cannot_read_or_change_a_search(client, psa):
    search = start(client).json()["search_id"]
    assert client.get(f"/api/searches/{search}").status_code == 200

    other = another_user(client)
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


def test_ip_daily_limit_applies_to_free_searches(tmp_path, psa):
    client = make_client(tmp_path, psa, searches_per_ip_per_day=2)
    assert start(client).status_code == 200
    assert start(another_user(client, "user-2", "b@example.com")).status_code == 200
    assert start(another_user(client, "user-3", "c@example.com")).status_code == 429
    assert len(psa.creates) == 2


def test_ip_daily_limit_does_not_apply_to_subscribers(tmp_path, psa):
    client = make_client(tmp_path, psa, searches_per_ip_per_day=1)
    subscribe(client)
    assert start(client).status_code == 200
    assert start(client).status_code == 200


def test_global_limit_counts_follow_ups(tmp_path, psa):
    client = make_client(tmp_path, psa, model_runs_per_day=2)
    search = start(client).json()["search_id"]
    assert client.post(f"/api/searches/{search}/find-more").status_code == 200
    assert client.post(f"/api/searches/{search}/find-more").status_code == 429
    assert start(another_user(client)).status_code == 429


def test_follow_up_limit_per_paid_search(tmp_path, psa):
    client = make_client(tmp_path, psa, follow_ups_per_search=1)
    subscribe(client)
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


def test_session_cookie_is_http_only(tmp_path, psa):
    client = make_client(tmp_path, psa, user=None)
    response = client.post("/api/session", json={"id_token": "good:user-1:alice@example.com"})
    assert "httponly" in response.headers["set-cookie"].lower()


def test_forwarded_ip_is_trusted_only_from_a_proxy():
    from types import SimpleNamespace

    from acadadviser.app import client_ip, trusted_proxies
    from acadadviser.config import Settings

    proxies = trusted_proxies(Settings(_env_file=None, trusted_proxy_cidrs="10.0.0.0/16"))

    def request(peer, forwarded):
        return SimpleNamespace(client=SimpleNamespace(host=peer), headers={"x-forwarded-for": forwarded})

    assert client_ip(request("10.0.3.7", "6.6.6.6, 203.0.113.9"), proxies) == "203.0.113.9"
    assert client_ip(request("198.51.100.4", "203.0.113.9"), proxies) == "198.51.100.4"


def test_hsts_only_when_served_over_https(tmp_path, psa):
    assert "strict-transport-security" not in make_client(tmp_path, psa).get("/").headers
    secure = make_client(tmp_path / "secure", psa, user=None, cookie_secure=True)
    assert secure.get("/").headers["strict-transport-security"] == "max-age=31536000"
