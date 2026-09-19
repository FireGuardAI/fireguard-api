from contextlib import asynccontextmanager
import json

import asyncpg
import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.security import OAuth2PasswordRequestForm
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.config import settings
from app.schemas import RegisterRequest, TextAnalyzeRequest, TokenResponse, UserResponse
from app.security import (
    create_access_token,
    current_user_id,
    hash_password,
    verify_password,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10)
    yield
    await app.state.db.close()


app = FastAPI(title=settings.api_title, version=settings.api_version, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    request_id = request.headers.get("x-request-id", "-")
    print(f"request_id={request_id} path={request.url.path} error={exc}")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
async def health() -> dict:
    try:
        async with app.state.db.acquire() as connection:
            await connection.fetchval("SELECT 1")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    return {"status": "ok", "service": settings.api_title}


@app.post("/auth/register", response_model=UserResponse, status_code=201)
@limiter.limit("5/minute")
async def register(request: Request, body: RegisterRequest) -> UserResponse:
    password_hash = hash_password(body.password)
    try:
        async with app.state.db.acquire() as connection:
            row = await connection.fetchrow(
                "INSERT INTO users (username, password_hash) VALUES ($1, $2) "
                "RETURNING id::text, username",
                body.username,
                password_hash,
            )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="Username already exists") from exc
    return UserResponse(id=row["id"], username=row["username"])


@app.post("/auth/token", response_model=TokenResponse)
@limiter.limit("10/minute")
async def login(request: Request, form: OAuth2PasswordRequestForm = Depends()) -> TokenResponse:
    async with app.state.db.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT id::text, username, password_hash, is_active FROM users WHERE username=$1",
            form.username,
        )
        valid = bool(row and row["is_active"] and verify_password(form.password, row["password_hash"]))
        await connection.execute(
            "INSERT INTO auth_logs (username, success, ip_address) VALUES ($1, $2, $3)",
            form.username,
            valid,
            request.client.host if request.client else None,
        )
    if not valid:
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    return TokenResponse(
        access_token=create_access_token(row["id"], row["username"]),
        expires_in=settings.jwt_expire_minutes * 60,
    )


@app.post("/analyze")
@limiter.limit(settings.analyze_rate_limit)
async def analyze(
    request: Request,
    user_id: str = Depends(current_user_id),
    file: UploadFile = File(...),
    building_name: str | None = Form(default=None, max_length=200),
    auditor_notes: str | None = Form(default=None, max_length=4000),
) -> Response:
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(status_code=415, detail="A PDF document is required")

    contents = await file.read(settings.max_upload_bytes + 1)
    if len(contents) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="Uploaded document is too large")
    if not contents:
        raise HTTPException(status_code=422, detail="Uploaded document is empty")

    headers = {"X-API-Key": settings.internal_api_key}
    multipart = {"file": (file.filename or "building.pdf", contents, file.content_type)}
    data = {
        key: value
        for key, value in {"building_name": building_name, "auditor_notes": auditor_notes}.items()
        if value is not None
    }
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            upstream = await client.post(
                f"{settings.intake_service_url.rstrip('/')}/api/v1/intake/document",
                files=multipart,
                data=data,
                headers=headers,
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(status_code=503, detail="Intake service unavailable") from exc

    if 200 <= upstream.status_code < 300:
        try:
            payload = upstream.json()
            report = payload.get("report") or payload
            external_session_id = report.get("session_id") if isinstance(report, dict) else None
            if external_session_id:
                async with app.state.db.acquire() as connection:
                    await connection.execute(
                        "INSERT INTO report_sessions "
                        "(user_id, external_session_id, metadata) VALUES ($1::uuid, $2, $3::jsonb) "
                        "ON CONFLICT (external_session_id) DO UPDATE SET last_active_at=now(), metadata=$3::jsonb",
                        user_id,
                        external_session_id,
                        json.dumps(payload),
                    )
        except Exception as exc:
            
            
            print(f"session metadata persistence failed: {exc}")

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


@app.post("/analyze/text")
@limiter.limit(settings.analyze_rate_limit)
async def analyze_text(
    request: Request,
    body: TextAnalyzeRequest,
    _user_id: str = Depends(current_user_id),
) -> Response:
    """Run plain text through the same downstream pipeline as a PDF upload."""
    headers = {"X-API-Key": settings.internal_api_key}
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            intake_response = await client.post(
                f"{settings.intake_service_url.rstrip('/')}/api/v1/intake",
                json={"raw_prompt": body.raw_prompt},
                headers=headers,
            )
            if intake_response.status_code >= 400:
                return Response(
                    content=intake_response.content,
                    status_code=intake_response.status_code,
                    media_type=intake_response.headers.get("content-type", "application/json"),
                )

            attributes = intake_response.json()
            attributes.update(
                {
                    key: value
                    for key, value in {
                        "building_name": body.building_name,
                        "auditor_notes": body.auditor_notes,
                    }.items()
                    if value is not None
                }
            )
            pipeline_response = await client.post(
                f"{settings.retrieval_service_url.rstrip('/')}/api/v1/pipeline/run",
                json=attributes,
                headers=headers,
            )
    except (ValueError, httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(status_code=503, detail="Analysis pipeline unavailable") from exc

    return Response(
        content=pipeline_response.content,
        status_code=pipeline_response.status_code,
        media_type=pipeline_response.headers.get("content-type", "application/json"),
    )
