"""HTTP API and staff web UI."""
from __future__ import annotations

import ipaddress
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import db
from .agent.live import TallyLive
from .agent.llm import OllamaLLM
from .agent.planner import Planner
from .auth import AuthError, RateLimiter, User, UserStore
from .config import settings
from .masking import MaskingEngine, RedisTokenVault
from .reports.registry import ReportError, catalogue_for_role
from .service import AgentService, AuditFailure, Denied

log = logging.getLogger("tally_agent")
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    r = aioredis.from_url(settings.redis_url)
    llm = OllamaLLM(settings.ollama_url, settings.ollama_model,
                    settings.llm_timeout_seconds) if settings.llm_enabled else None
    app.state.redis = r
    app.state.llm = llm
    app.state.users = UserStore.load(settings.users_file)
    app.state.users_mtime = os.stat(settings.users_file).st_mtime_ns
    app.state.users_error = None
    app.state.limiter = RateLimiter(r, settings.rate_limit_per_minute)
    app.state.service = AgentService(
        engine=MaskingEngine(RedisTokenVault(r, settings.token_ttl_seconds)),
        planner=Planner(llm),
        llm=llm,
        live=TallyLive(settings.tally_live_url, settings.tally_live_timeout),
        redis_client=r,
        max_rows=settings.max_rows,
        cache_ttl=settings.cache_ttl_seconds,
        narrate=settings.llm_narrate,
        audit_salt=settings.audit_hash_salt,
        audit_fail_closed=settings.audit_fail_closed,
        stale_warn_minutes=settings.sync_stale_warn_minutes,
        reference_date=settings.reference_date,
    )
    await db.get_pool()
    yield
    await db.close_pool()
    if llm is not None:
        await llm.aclose()
    await r.aclose()


app = FastAPI(title="Tally Agent", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    return response


# ------------------------------------------------------------------ auth
def _users(app_: FastAPI) -> UserStore:
    """Reload the user list when the file changes, so removing someone's entry
    revokes their access on their next request -- no restart needed.

    If the edited file is invalid, access is REFUSED for everyone until it is
    fixed: keeping the previous list would keep a just-removed person in."""
    try:
        mtime = os.stat(settings.users_file).st_mtime_ns
    except OSError:
        mtime = None
    if mtime != app_.state.users_mtime:
        app_.state.users_mtime = mtime
        try:
            app_.state.users = UserStore.load(settings.users_file)
            app_.state.users_error = None
            log.warning("users file reloaded")
        except Exception as exc:  # noqa: BLE001 -- any problem must fail closed
            app_.state.users_error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            log.error("users file invalid, refusing all requests: %s", app_.state.users_error)
    if app_.state.users_error:
        raise UsersFileError(app_.state.users_error)
    return app_.state.users


class UsersFileError(Exception):
    pass


async def current_user(request: Request) -> User:
    header = request.headers.get("authorization", "")
    key = header[7:].strip() if header.lower().startswith("bearer ") else None
    try:
        user = _users(request.app).authenticate(key, request.headers.get("x-act-as"))
        await request.app.state.limiter.check(user.audit_id)
        if user.acting_via:
            # The bridge's own budget too: a leaked bridge key must not get the
            # limit once per impersonated person.
            await request.app.state.limiter.check(f"principal:{user.acting_via}")
    except UsersFileError as exc:
        raise HTTPException(status_code=503, detail="access list is being updated or is "
                            "invalid; ask the administrator") from exc
    except AuthError as exc:
        status = 429 if "rate limit" in str(exc) else 401
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return user


def _ip(request: Request) -> str | None:
    # Caddy is the only client that can reach us (bound to 127.0.0.1 / internal
    # network), so its X-Forwarded-For is trusted; anything else uses the peer.
    fwd = request.headers.get("x-forwarded-for")
    raw = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return None


# ---------------------------------------------------------------- models
class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    # Optional: defaults to the user's first company (used by the Telegram bot).
    company_id: int | None = None
    session_id: str | None = Field(default=None, max_length=64)


class ReportIn(BaseModel):
    report_id: str = Field(max_length=64)
    company_id: int
    params: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = Field(default=None, max_length=64)


def _guard(exc: Exception) -> HTTPException:
    if isinstance(exc, Denied):
        return HTTPException(403, str(exc))
    if isinstance(exc, ReportError):
        return HTTPException(400, str(exc))
    if isinstance(exc, AuditFailure):
        return HTTPException(503, str(exc))
    ref = uuid.uuid4().hex[:12]
    # Log the type and reference only: messages may contain client data.
    log.error("unhandled error ref=%s type=%s", ref, type(exc).__name__)
    return HTTPException(500, f"internal error (ref {ref})")


# ---------------------------------------------------------------- routes
@app.post("/v1/ask")
async def ask(body: AskIn, request: Request, user: User = Depends(current_user)):
    try:
        res = await request.app.state.service.ask(
            user, body.question, body.company_id, body.session_id, _ip(request))
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc
    return res.__dict__


@app.post("/v1/report")
async def report(body: ReportIn, request: Request, user: User = Depends(current_user)):
    try:
        res = await request.app.state.service.run_direct(
            user, body.report_id, body.company_id, body.params, body.session_id, _ip(request))
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc
    return res.__dict__


@app.get("/v1/reports")
async def reports(user: User = Depends(current_user)):
    return [
        {"id": r.id, "title": r.title, "description": r.description,
         "params": [{"name": p.name, "type": p.type, "required": p.required,
                     "default": p.default} for p in r.params if not p.internal]}
        for r in catalogue_for_role(user.role)
    ]


@app.get("/v1/me")
async def me(request: Request, user: User = Depends(current_user)):
    companies = []
    for cid in user.companies:
        try:
            ctx = await request.app.state.service.company_context(cid)
            companies.append({"company_id": cid, "name": ctx.name,
                              "lag_minutes": ctx.lag_minutes, "sync_status": ctx.sync_status})
        except Denied:
            continue
    return {"id": user.audit_id, "role": user.role, "may_unmask": user.may_unmask,
            "companies": companies}


@app.get("/healthz")
async def healthz(request: Request):
    status: dict[str, Any] = {}
    try:
        pool = await db.get_pool()
        async with pool.acquire() as con:
            h = await con.fetchrow("SELECT * FROM tally.fn_sync_health()")
        status["db"] = True
        status["sync"] = {"companies": h["companies"], "failed": h["failed"],
                          "max_lag_minutes": float(h["max_lag_minutes"])
                          if h["max_lag_minutes"] is not None else None}
    except Exception:  # noqa: BLE001
        status["db"] = False
    try:
        status["redis"] = bool(await request.app.state.redis.ping())
    except Exception:  # noqa: BLE001
        status["redis"] = False
    llm = request.app.state.llm
    status["llm"] = await llm.health() if llm is not None else False
    try:
        _users(request.app)
        status["users_file"] = "ok"
    except UsersFileError:
        status["users_file"] = "invalid"
    # The model is optional: without it the keyword router and report forms still work.
    status["status"] = ("ok" if status["db"] and status["redis"] and status["users_file"] == "ok"
                        else "down")
    sync = status.get("sync") or {}
    stale = (sync.get("failed") or 0) > 0 or (
        sync.get("max_lag_minutes") is not None
        and sync["max_lag_minutes"] > settings.sync_stale_warn_minutes)
    if status["status"] == "ok" and (not status["llm"] or stale):
        status["status"] = "degraded"
    return JSONResponse(status, status_code=200 if status["status"] != "down" else 503)


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/static/{name}")
async def static(name: str):
    allowed = {"app.js": "application/javascript", "app.css": "text/css"}
    if name not in allowed:
        raise HTTPException(404)
    return FileResponse(STATIC / name, media_type=allowed[name])
