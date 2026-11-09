"""The only public service. SN, SIP and PSA stay on loopback behind it."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from acadadviser.auth import CognitoVerifier, Identity, InvalidToken, email_key
from acadadviser.billing import Billing, InvalidEvent, interpret
from acadadviser.catalog import Catalog
from acadadviser.config import Settings, get_settings
from acadadviser.db import Database, LimitReached, PaymentRequired, User
from acadadviser.events import public_item, translate
from acadadviser.pages import render
from acadadviser.profile_doc import typed_profile_docx

_log = logging.getLogger("acadadviser")
_SITE = Path(__file__).resolve().parent / "site"
_COOKIE = "aa_account"
_SESSION_DAYS = 30
_MAX_BODY_BYTES = 25 * 1024 * 1024
_MAX_STREAM_SECONDS = 15 * 60
_CREATE_TIMEOUT = httpx.Timeout(300.0, connect=5.0)
_STREAM_TIMEOUT = httpx.Timeout(60.0, connect=5.0)
_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_SECURITY_HEADERS = {
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class MessageBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


class DropBody(BaseModel):
    web_id: str = Field(..., min_length=1, max_length=128)


class SessionBody(BaseModel):
    id_token: str = Field(..., min_length=1, max_length=8192)


class CheckoutBody(BaseModel):
    renewal_consent: bool = False


def content_security_policy(settings: Settings) -> str:
    # The sign-in form calls the Cognito API straight from the browser.
    cognito = f" https://cognito-idp.{settings.cognito_region}.amazonaws.com"
    return (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        f"connect-src 'self'{cognito}; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )


def trusted_proxies(settings: Settings) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [ipaddress.ip_network(cidr.strip()) for cidr in settings.trusted_proxy_cidrs.split(",") if cidr.strip()]


def client_ip(request: Request, proxies: list) -> str:
    """The proxy appends the address it saw, so the last X-Forwarded-For entry is trusted from a proxy only."""
    peer = request.client.host if request.client else ""
    try:
        from_proxy = peer == "testclient" or any(ipaddress.ip_address(peer) in net for net in proxies)
    except ValueError:
        from_proxy = False
    forwarded = request.headers.get("x-forwarded-for", "")
    if from_proxy and forwarded:
        return forwarded.split(",")[-1].strip()
    return peer


def _document_error(name: str, content: bytes, settings: Settings) -> str | None:
    if len(content) > settings.max_file_bytes:
        return f"{name} is larger than {settings.max_file_bytes // (1024 * 1024)} MB."
    if not content.startswith((_PDF_MAGIC, _ZIP_MAGIC)):
        return f"{name} must be a PDF or .docx file."
    return None


def _upstream_failure(response: httpx.Response) -> HTTPException:
    """Upstream detail can carry internals, so visitors get a fixed message."""
    _log.warning("upstream status=%s", response.status_code)
    if response.status_code == 400:
        return HTTPException(
            status_code=400,
            detail="We could not read that profile. Please check the file and try again.",
        )
    return HTTPException(
        status_code=502, detail="The recommendation service is unavailable. Please try again shortly."
    )


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    catalog: Catalog | None = None,
    verifier: Any = None,
    billing: Billing | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="AcadAdviserAI", docs_url=None, redoc_url=None, openapi_url=None)
    database = Database(settings.database_target())
    proxies = trusted_proxies(settings)
    catalog = catalog or Catalog(settings.sip_url)
    uploads = asyncio.Semaphore(settings.concurrent_uploads)
    csp = content_security_policy(settings)
    if verifier is None and settings.cognito_user_pool_id and settings.cognito_client_id:
        verifier = CognitoVerifier(
            settings.cognito_region, settings.cognito_user_pool_id, settings.cognito_client_id
        )
    billing = billing or Billing(
        settings.stripe_secret_key, settings.stripe_webhook_secret, settings.stripe_price_id
    )

    def client(timeout: httpx.Timeout = _STREAM_TIMEOUT) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=settings.psa_url, timeout=timeout, transport=transport)

    def page(name: str, active: str) -> HTMLResponse:
        content = render(name, active, settings.contact_email)
        return HTMLResponse(content, headers={"Cache-Control": "no-cache"})

    def current_user(request: Request) -> User | None:
        return database.session_user(request.cookies.get(_COOKIE))

    def require_user(request: Request) -> User:
        user = current_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="Please sign in to continue.")
        return user

    def owned(request: Request, thread_id: str) -> User:
        user = require_user(request)
        # The same 404 for missing and foreign searches, so ids cannot be probed.
        if not database.owns(thread_id, user):
            raise HTTPException(status_code=404, detail="Search not found.")
        return user

    def base_url(request: Request) -> str:
        if settings.site_domain and settings.cookie_secure:
            return f"https://{settings.site_domain}"
        return str(request.base_url).rstrip("/")

    async def enrich(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return await asyncio.to_thread(catalog.enrich, [public_item(item) for item in items])

    async def latest_run_id(thread_id: str) -> str | None:
        async with client() as http:
            response = await http.get(f"/v1/threads/{thread_id}")
        if response.status_code != 200:
            raise _upstream_failure(response)
        return response.json().get("latest_run_id")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > _MAX_BODY_BYTES:
            return JSONResponse({"detail": "Upload is too large."}, status_code=413)
        if request.method == "POST":
            origin = request.headers.get("origin")
            allowed = {f"https://{settings.site_domain}", f"https://www.{settings.site_domain}"}
            allowed.add(str(request.base_url).rstrip("/"))
            if origin and origin not in allowed:
                return JSONResponse({"detail": "Cross-site request rejected."}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", csp)
        if settings.cookie_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        for key, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    @app.exception_handler(LimitReached)
    async def limit_reached(request: Request, exc: LimitReached):
        return JSONResponse({"detail": str(exc)}, status_code=429)

    @app.exception_handler(PaymentRequired)
    async def payment_required(request: Request, exc: PaymentRequired):
        return JSONResponse({"detail": str(exc), "subscribe": "/account"}, status_code=402)

    @app.exception_handler(httpx.HTTPError)
    async def upstream_unreachable(request: Request, exc: httpx.HTTPError):
        _log.warning("upstream error: %s", type(exc).__name__)
        return JSONResponse(
            {"detail": "The recommendation service is unavailable. Please try again shortly."},
            status_code=503,
        )

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    def home() -> HTMLResponse:
        return page("home.html", "home")

    @app.api_route("/advisor", methods=["GET", "HEAD"], include_in_schema=False)
    def advisor() -> HTMLResponse:
        return page("advisor.html", "advisor")

    @app.api_route("/signin", methods=["GET", "HEAD"], include_in_schema=False)
    def signin() -> HTMLResponse:
        return page("signin.html", "signin")

    @app.api_route("/account", methods=["GET", "HEAD"], include_in_schema=False)
    def account_page() -> HTMLResponse:
        return page("account.html", "account")

    # Pages from the old static site.
    @app.get("/signin.html", include_in_schema=False)
    @app.get("/account.html", include_in_schema=False)
    def old_page(request: Request) -> RedirectResponse:
        return RedirectResponse(request.url.path.removesuffix(".html"), status_code=301)

    @app.get("/static/auth-config.json", include_in_schema=False)
    def auth_config() -> dict[str, str]:
        if verifier is None:
            raise HTTPException(status_code=404, detail="Sign-in is not set up.")
        return {"region": settings.cognito_region, "clientId": settings.cognito_client_id}

    @app.api_route("/privacy", methods=["GET", "HEAD"], include_in_schema=False)
    def privacy() -> HTMLResponse:
        return page("privacy.html", "privacy")

    @app.api_route("/terms", methods=["GET", "HEAD"], include_in_schema=False)
    def terms() -> HTMLResponse:
        return page("terms.html", "terms")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/session")
    async def create_session(body: SessionBody) -> JSONResponse:
        if verifier is None:
            raise HTTPException(status_code=503, detail="Sign-in is not available right now.")
        try:
            identity: Identity = await asyncio.to_thread(verifier.verify, body.id_token)
        except InvalidToken as exc:
            _log.info("rejected sign-in token: %s", exc)
            raise HTTPException(status_code=401, detail="Your sign-in could not be confirmed. Please sign in again.")
        token = database.sign_in(identity.sub, identity.email, _SESSION_DAYS)
        response = JSONResponse({"email": identity.email})
        response.set_cookie(
            _COOKIE, token, max_age=_SESSION_DAYS * 24 * 60 * 60, httponly=True,
            secure=settings.cookie_secure, samesite="lax",
        )
        return response

    @app.post("/api/session/sign-out")
    def sign_out(request: Request) -> JSONResponse:
        if token := request.cookies.get(_COOKIE):
            database.sign_out(token)
        response = JSONResponse({"status": "ok"})
        response.delete_cookie(_COOKIE, httponly=True, secure=settings.cookie_secure, samesite="lax")
        return response

    @app.get("/api/account")
    def account(request: Request) -> dict[str, Any]:
        user = require_user(request)
        return {
            "email": user.email,
            "price": settings.plan_price_label,
            "billing_available": billing.enabled,
            "has_billing_account": bool(user.stripe_customer_id),
            "free_follow_ups": settings.free_follow_ups,
            "follow_ups_per_search": settings.follow_ups_per_search,
            **database.account(user.sub, email_key(user.email)),
        }

    @app.post("/api/billing/checkout")
    async def checkout(request: Request, body: CheckoutBody) -> dict[str, str]:
        user = require_user(request)
        if not billing.enabled:
            raise HTTPException(status_code=503, detail="Subscriptions are not open yet.")
        if database.account(user.sub, email_key(user.email))["subscribed"]:
            raise HTTPException(status_code=409, detail="You already have a subscription.")
        if not body.renewal_consent:
            raise HTTPException(
                status_code=400, detail="Please confirm that the plan renews every year until you cancel."
            )
        database.record_renewal_consent(user.sub)
        url = await asyncio.to_thread(
            billing.checkout_url, user_sub=user.sub, email=user.email,
            customer_id=user.stripe_customer_id, base_url=base_url(request),
        )
        return {"url": url}

    @app.post("/api/billing/portal")
    async def billing_portal(request: Request) -> dict[str, str]:
        user = require_user(request)
        if not billing.enabled:
            raise HTTPException(status_code=503, detail="Billing is not available right now.")
        if not user.stripe_customer_id:
            raise HTTPException(status_code=404, detail="There is no billing account yet.")
        url = await asyncio.to_thread(billing.portal_url, user.stripe_customer_id, base_url(request))
        return {"url": url}

    # Stripe sends no Origin header, so the cross-site guard lets it through; the signature is the check.
    @app.post("/api/stripe/webhook", include_in_schema=False)
    async def stripe_webhook(request: Request) -> dict[str, bool]:
        if not billing.enabled:
            raise HTTPException(status_code=503, detail="Billing is not configured.")
        try:
            event = billing.parse_event(await request.body(), request.headers.get("stripe-signature", ""))
        except InvalidEvent as exc:
            _log.warning("rejected stripe webhook: %s", exc)
            raise HTTPException(status_code=400, detail="Invalid signature.")
        update = interpret(event)
        if update.country and update.country != "US":
            _log.warning("checkout from outside the US: event=%s country=%s", update.event_id, update.country)
        database.apply_stripe_update(update)
        return {"received": True}

    @app.post("/api/searches")
    async def create_search(
        request: Request,
        consent: str = Form(""),
        background: str = Form(""),
        resume: UploadFile | None = File(None),
        transcript: UploadFile | None = File(None),
        intended_majors: str = Form(""),
        degree_preferences: str = Form(""),
        geographic_preferences: str = Form(""),
        financial_constraints: str = Form(""),
        career_goals: str = Form(""),
        gre: str = Form(""),
        gmat: str = Form(""),
        toefl: str = Form(""),
        ielts: str = Form(""),
    ) -> JSONResponse:
        user = require_user(request)
        if consent != "yes":
            raise HTTPException(status_code=400, detail="Please accept the data notice to continue.")
        fields = {
            "intended_majors": intended_majors, "degree_preferences": degree_preferences,
            "geographic_preferences": geographic_preferences,
            "financial_constraints": financial_constraints, "career_goals": career_goals,
            "gre": gre, "gmat": gmat, "toefl": toefl, "ielts": ielts,
        }
        if any(len(value) > settings.max_text_chars for value in [background, *fields.values()]):
            raise HTTPException(status_code=400, detail="One of the text fields is too long.")

        resume_name = resume.filename if resume and resume.filename else None
        resume_bytes = await resume.read() if resume_name else b""
        transcript_name = transcript.filename if transcript and transcript.filename else None
        transcript_bytes = await transcript.read() if transcript_name else b""
        if resume_bytes:
            if error := _document_error("Your resume", resume_bytes, settings):
                raise HTTPException(status_code=400, detail=error)
        elif background.strip():
            resume_name, resume_bytes = "typed-profile.docx", typed_profile_docx(background)
        else:
            raise HTTPException(status_code=400, detail="Upload a resume or describe your background.")
        if transcript_bytes:
            if error := _document_error("Your transcript", transcript_bytes, settings):
                raise HTTPException(status_code=400, detail=error)

        if uploads.locked():
            raise HTTPException(status_code=503, detail="We are busy right now. Please try again in a minute.")
        async with uploads:
            plan = database.reserve_search(
                user, email_key(user.email), client_ip(request, proxies),
                global_limit=settings.model_runs_per_day, ip_limit=settings.searches_per_ip_per_day,
                user_limit=settings.searches_per_user_per_day,
            )
            # student_id and session_id are never forwarded: PSA mints a fresh id per search.
            files: list[tuple[str, tuple[str, bytes]]] = [("resume", (resume_name, resume_bytes))]
            if transcript_bytes:
                files.append(("transcript", (transcript_name, transcript_bytes)))
            data = {key: value.strip() for key, value in fields.items() if value.strip()}
            try:
                async with client(_CREATE_TIMEOUT) as http:
                    upstream = await http.post("/v1/threads", data=data, files=files)
            except httpx.HTTPError:
                database.release_search(user, plan)
                raise
        if upstream.status_code != 200:
            database.release_search(user, plan)
            raise _upstream_failure(upstream)

        thread_id = upstream.json()["thread_id"]
        database.add_search(thread_id, user, plan)
        return JSONResponse({"search_id": thread_id, "plan": plan})

    @app.get("/api/searches/{thread_id}")
    async def show_search(request: Request, thread_id: str) -> dict[str, Any]:
        owned(request, thread_id)
        async with client() as http:
            response = await http.get(f"/v1/threads/{thread_id}")
        if response.status_code != 200:
            raise _upstream_failure(response)
        run = response.json().get("latest_run") or {}
        return {
            "search_id": thread_id,
            "run_id": run.get("run_id"),
            "items": await enrich(run.get("items") or []),
        }

    @app.get("/api/searches/{thread_id}/events")
    async def search_events(request: Request, thread_id: str) -> StreamingResponse:
        owned(request, thread_id)

        async def stream() -> AsyncIterator[str]:
            try:
                async with asyncio.timeout(_MAX_STREAM_SECONDS), client() as http, http.stream(
                    "GET", f"/v1/threads/{thread_id}/events"
                ) as upstream:
                    async for line in upstream.aiter_lines():
                        if line.startswith(":"):
                            yield ": ping\n\n"
                        elif line.startswith("data: "):
                            event = translate(json.loads(line[6:]))
                            if event is None:
                                continue
                            if event["type"] == "list":
                                event["items"] = await enrich(event["items"])
                            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except (httpx.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
                _log.warning("event stream ended: %s", type(exc).__name__)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def run_follow_up(request: Request, thread_id: str, path: str, **kwargs: Any) -> dict[str, str]:
        user = owned(request, thread_id)
        database.reserve_follow_up(
            thread_id, user, client_ip(request, proxies), global_limit=settings.model_runs_per_day,
            free_limit=settings.free_follow_ups, paid_limit=settings.follow_ups_per_search,
        )
        async with client() as http:
            response = await http.post(f"/v1/threads/{thread_id}/{path}", **kwargs)
        if response.status_code != 200:
            raise _upstream_failure(response)
        return {"status": "ok"}

    @app.post("/api/searches/{thread_id}/messages")
    async def send_message(request: Request, thread_id: str, body: MessageBody) -> dict[str, str]:
        return await run_follow_up(request, thread_id, "messages", json={"text": body.text})

    @app.post("/api/searches/{thread_id}/find-more")
    async def find_more(request: Request, thread_id: str) -> dict[str, str]:
        return await run_follow_up(request, thread_id, "find-more")

    @app.post("/api/searches/{thread_id}/drop")
    async def drop_program(request: Request, thread_id: str, body: DropBody) -> dict[str, Any]:
        owned(request, thread_id)
        reaction = {"kind": "exclude_program", "program_id": body.web_id}
        async with client() as http:
            response = await http.post(
                f"/v1/threads/{thread_id}/reactions", json={"reactions": [reaction]}
            )
        if response.status_code != 200:
            raise _upstream_failure(response)
        return {"items": await enrich(response.json().get("items") or [])}

    @app.get("/api/searches/{thread_id}/report")
    async def report(request: Request, thread_id: str) -> Response:
        owned(request, thread_id)
        run_id = await latest_run_id(thread_id)
        if not run_id:
            raise HTTPException(status_code=404, detail="No list is ready yet.")
        async with client() as http:
            response = await http.get(f"/v1/runs/{run_id}/report")
        if response.status_code != 200:
            raise _upstream_failure(response)
        return Response(
            response.content,
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="AcadAdviserAI-program-list.html"',
                "Cache-Control": "no-store",
            },
        )

    app.mount("/static", StaticFiles(directory=_SITE), name="static")
    return app


app = create_app()
