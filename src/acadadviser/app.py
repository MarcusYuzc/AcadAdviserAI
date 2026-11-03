"""The only public service. SN, SIP and PSA stay on loopback behind it."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from acadadviser.catalog import Catalog
from acadadviser.config import Settings, get_settings
from acadadviser.db import Database, LimitReached
from acadadviser.events import public_item, translate
from acadadviser.pages import render
from acadadviser.profile_doc import typed_profile_docx

_log = logging.getLogger("acadadviser")
_SITE = Path(__file__).resolve().parent / "site"
_COOKIE = "aa_session"
_COOKIE_MAX_AGE = 60 * 60 * 24 * 30
_LOOPBACK = {"127.0.0.1", "::1", "testclient"}
_MAX_BODY_BYTES = 25 * 1024 * 1024
_MAX_STREAM_SECONDS = 15 * 60
_CREATE_TIMEOUT = httpx.Timeout(300.0, connect=5.0)
_STREAM_TIMEOUT = httpx.Timeout(60.0, connect=5.0)
_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class MessageBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


class DropBody(BaseModel):
    web_id: str = Field(..., min_length=1, max_length=128)


def client_ip(request: Request) -> str:
    """Caddy overwrites X-Forwarded-For, so it is only trusted from a loopback peer."""
    peer = request.client.host if request.client else ""
    if peer in _LOOPBACK:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
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
) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="AcadAdviserAI", docs_url=None, redoc_url=None, openapi_url=None)
    database = Database(settings.data_dir / "gateway.sqlite3")
    catalog = catalog or Catalog(settings.sip_url)
    uploads = asyncio.Semaphore(settings.concurrent_uploads)

    def client(timeout: httpx.Timeout = _STREAM_TIMEOUT) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=settings.psa_url, timeout=timeout, transport=transport)

    def page(name: str, active: str) -> HTMLResponse:
        content = render(name, active, settings.contact_email)
        return HTMLResponse(content, headers={"Cache-Control": "no-cache"})

    def owned(request: Request, thread_id: str) -> None:
        # The same 404 for missing and foreign searches, so ids cannot be probed.
        if not database.owns(thread_id, request.cookies.get(_COOKIE)):
            raise HTTPException(status_code=404, detail="Search not found.")

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
        for key, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    @app.exception_handler(LimitReached)
    async def limit_reached(request: Request, exc: LimitReached):
        return JSONResponse({"detail": str(exc)}, status_code=429)

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

    # Local stack only, which has no sign-in yet. The public site signs in through Cognito.
    @app.get("/signin", include_in_schema=False)
    def signin() -> RedirectResponse:
        return RedirectResponse("/advisor", status_code=302)

    @app.api_route("/privacy", methods=["GET", "HEAD"], include_in_schema=False)
    def privacy() -> HTMLResponse:
        return page("privacy.html", "privacy")

    @app.api_route("/terms", methods=["GET", "HEAD"], include_in_schema=False)
    def terms() -> HTMLResponse:
        return page("terms.html", "terms")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

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
            database.reserve_search(
                client_ip(request), settings.model_runs_per_day, settings.searches_per_ip_per_day
            )
            # student_id and session_id are never forwarded: PSA mints a fresh id per search.
            files: list[tuple[str, tuple[str, bytes]]] = [("resume", (resume_name, resume_bytes))]
            if transcript_bytes:
                files.append(("transcript", (transcript_name, transcript_bytes)))
            data = {key: value.strip() for key, value in fields.items() if value.strip()}
            async with client(_CREATE_TIMEOUT) as http:
                upstream = await http.post("/v1/threads", data=data, files=files)
        if upstream.status_code != 200:
            raise _upstream_failure(upstream)

        thread_id = upstream.json()["thread_id"]
        token = request.cookies.get(_COOKIE) or secrets.token_urlsafe(32)
        database.add_search(thread_id, token)
        response = JSONResponse({"search_id": thread_id})
        response.set_cookie(
            _COOKIE, token, max_age=_COOKIE_MAX_AGE, httponly=True,
            secure=settings.cookie_secure, samesite="lax",
        )
        return response

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
        owned(request, thread_id)
        database.reserve_follow_up(
            thread_id, client_ip(request), settings.model_runs_per_day, settings.follow_ups_per_search
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
