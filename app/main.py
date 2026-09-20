from contextlib import asynccontextmanager
from datetime import date
import json
import re
from uuid import UUID, uuid4

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
from app.schemas import (
    AccountResponse,
    MockPaymentConfirmRequest,
    MockPaymentConfirmResponse,
    PlanName,
    PaymentCheckoutRequest,
    PaymentCheckoutResponse,
    RegisterRequest,
    TextAnalyzeRequest,
    TokenResponse,
    UserResponse,
)
from app.security import (
    create_access_token,
    current_user_id,
    hash_password,
    verify_password,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10)
    await _ensure_plan_schema(app.state.db)
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


PLAN_FEATURES = {
    PlanName.STUDENT: {"pdf": False, "chatbot": False, "api": False, "payment": False},
    PlanName.BASIC: {"pdf": True, "chatbot": False, "api": False, "payment": True},
    PlanName.PRO: {"pdf": True, "chatbot": True, "api": False, "payment": False},
    PlanName.ENTERPRISE: {"pdf": True, "chatbot": True, "api": True, "payment": False},
    PlanName.GOVERNMENT: {"pdf": True, "chatbot": True, "api": True, "payment": False},
}

MOCK_PLAN_PRICES = {
    PlanName.BASIC: 2500,
    PlanName.PRO: 7500,
    PlanName.ENTERPRISE: 25000,
    PlanName.GOVERNMENT: 0,
}


async def _ensure_plan_schema(pool) -> None:
    async with pool.acquire() as connection:
        await connection.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(254), "
            "ADD COLUMN IF NOT EXISTS plan VARCHAR(20) NOT NULL DEFAULT 'student', "
            "ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE, "
            "ADD COLUMN IF NOT EXISTS report_credits INTEGER NOT NULL DEFAULT 0, "
            "ADD COLUMN IF NOT EXISTS subscription_status VARCHAR(20) NOT NULL DEFAULT 'inactive'"
        )
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS monthly_report_usage ("
            "user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
            "usage_month DATE NOT NULL, report_count INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY (user_id, usage_month))"
        )
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS mock_payment_transactions ("
            "checkout_id UUID PRIMARY KEY, "
            "user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
            "plan VARCHAR(20) NOT NULL, "
            "amount INTEGER NOT NULL, "
            "currency VARCHAR(3) NOT NULL DEFAULT 'LKR', "
            "status VARCHAR(20) NOT NULL DEFAULT 'pending', "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
            "completed_at TIMESTAMPTZ)"
        )


def _institutional_email(email: str | None) -> bool:
    if not email or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return False
    domain = email.rsplit("@", 1)[1].lower()
    return any(domain == allowed.lower() or domain.endswith("." + allowed.lower()) for allowed in settings.institutional_email_domains)


async def _account(pool, user_id: str):
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT id::text, username, email, plan, email_verified, report_credits, "
            "subscription_status FROM users WHERE id=$1::uuid AND is_active=TRUE",
            user_id,
        )
        if row is None:
            raise HTTPException(status_code=401, detail="User account is unavailable")
        usage = await connection.fetchval(
            "SELECT report_count FROM monthly_report_usage WHERE user_id=$1::uuid AND usage_month=$2",
            user_id,
            date.today().replace(day=1),
        ) or 0
    return row, int(usage)


async def _reserve_report(pool, user_id: str, is_pdf: bool) -> str:
    async with pool.acquire() as connection:
        async with connection.transaction():
            row = await connection.fetchrow(
                "SELECT plan, email, email_verified, report_credits, subscription_status "
                "FROM users WHERE id=$1::uuid AND is_active=TRUE FOR UPDATE",
                user_id,
            )
            if row is None:
                raise HTTPException(status_code=401, detail="User account is unavailable")
            try:
                plan = PlanName(row["plan"])
            except ValueError as exc:
                raise HTTPException(status_code=403, detail="Unsupported account plan") from exc
            features = PLAN_FEATURES[plan]
            if plan == PlanName.STUDENT and not _institutional_email(row["email"]):
                raise HTTPException(status_code=403, detail="Student Plan requires a verified institutional email")
            if plan == PlanName.STUDENT and not row["email_verified"]:
                raise HTTPException(status_code=403, detail="Verify your institutional email before generating reports")
            if is_pdf and not features["pdf"]:
                raise HTTPException(status_code=403, detail="PDF analysis is not available on the Student Plan")
            if plan == PlanName.BASIC and row["report_credits"] < 1:
                raise HTTPException(status_code=402, detail="Payment required: purchase a report credit on the Basic Plan")
            if plan == PlanName.PRO and row["subscription_status"] != "active":
                raise HTTPException(status_code=402, detail="An active Pro subscription is required")
            if plan in (PlanName.ENTERPRISE, PlanName.GOVERNMENT) and row["subscription_status"] != "active":
                raise HTTPException(status_code=402, detail="Complete payment to activate this plan")
            if plan == PlanName.STUDENT:
                month = date.today().replace(day=1)
                count = await connection.fetchval(
                    "SELECT report_count FROM monthly_report_usage WHERE user_id=$1::uuid AND usage_month=$2 FOR UPDATE",
                    user_id,
                    month,
                ) or 0
                if count >= settings.student_monthly_report_limit:
                    raise HTTPException(status_code=429, detail="Student Plan monthly report limit reached")
                await connection.execute(
                    "INSERT INTO monthly_report_usage(user_id, usage_month, report_count) VALUES($1::uuid,$2,1) "
                    "ON CONFLICT(user_id, usage_month) DO UPDATE SET report_count=monthly_report_usage.report_count+1",
                    user_id,
                    month,
                )
            elif plan == PlanName.BASIC:
                await connection.execute("UPDATE users SET report_credits=report_credits-1 WHERE id=$1::uuid", user_id)
            return plan.value


async def _release_report(pool, user_id: str, plan: str) -> None:
    async with pool.acquire() as connection:
        if plan == PlanName.STUDENT.value:
            await connection.execute(
                "UPDATE monthly_report_usage SET report_count=GREATEST(report_count-1, 0) "
                "WHERE user_id=$1::uuid AND usage_month=$2",
                user_id,
                date.today().replace(day=1),
            )
        elif plan == PlanName.BASIC.value:
            await connection.execute("UPDATE users SET report_credits=report_credits+1 WHERE id=$1::uuid", user_id)


async def _finish_report(pool, user_id: str, plan: str, response: httpx.Response) -> None:
    if response.status_code < 200 or response.status_code >= 300:
        await _release_report(pool, user_id, plan)
        return
    try:
        payload = response.json()
    except ValueError:
        await _release_report(pool, user_id, plan)
        return
    if not payload.get("report"):
        await _release_report(pool, user_id, plan)


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
    email = body.email.lower().strip() if body.email else None
    if not email or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=422, detail="A valid email address is required")
    if body.plan == PlanName.STUDENT and not _institutional_email(email):
        raise HTTPException(
            status_code=422,
            detail="Student registration requires an institutional email from an approved domain",
        )
    if body.plan != PlanName.STUDENT and not settings.mock_payments_enabled:
        raise HTTPException(status_code=503, detail="Mock payments are disabled")

    checkout_id = str(uuid4()) if body.plan != PlanName.STUDENT else None
    try:
        async with app.state.db.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    "INSERT INTO users (username, email, password_hash, plan, email_verified) "
                    "VALUES ($1, $2, $3, $4, TRUE) "
                    "RETURNING id::text, username, plan, email_verified",
                    body.username,
                    email,
                    password_hash,
                    body.plan.value,
                )
                if checkout_id:
                    await connection.execute(
                        "INSERT INTO mock_payment_transactions "
                        "(checkout_id, user_id, plan, amount) VALUES ($1::uuid, $2::uuid, $3, $4)",
                        checkout_id,
                        row["id"],
                        body.plan.value,
                        MOCK_PLAN_PRICES[body.plan],
                    )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="Username or email already exists") from exc
    return UserResponse(
        id=row["id"],
        username=row["username"],
        plan=PlanName(row["plan"]),
        email_verified=row["email_verified"],
        payment_required=checkout_id is not None,
        checkout_id=checkout_id,
        amount=MOCK_PLAN_PRICES[body.plan] if checkout_id else None,
    )


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
    reserved_plan = await _reserve_report(app.state.db, user_id, is_pdf=True)
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        await _release_report(app.state.db, user_id, reserved_plan)
        raise HTTPException(status_code=415, detail="A PDF document is required")

    contents = await file.read(settings.max_upload_bytes + 1)
    if len(contents) > settings.max_upload_bytes:
        await _release_report(app.state.db, user_id, reserved_plan)
        raise HTTPException(status_code=413, detail="Uploaded document is too large")
    if not contents:
        await _release_report(app.state.db, user_id, reserved_plan)
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
        await _release_report(app.state.db, user_id, reserved_plan)
        raise HTTPException(status_code=503, detail="Intake service unavailable") from exc

    await _finish_report(app.state.db, user_id, reserved_plan, upstream)

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
    reserved_plan = await _reserve_report(app.state.db, _user_id, is_pdf=False)
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
                await _release_report(app.state.db, _user_id, reserved_plan)
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
        await _release_report(app.state.db, _user_id, reserved_plan)
        raise HTTPException(status_code=503, detail="Analysis pipeline unavailable") from exc

    await _finish_report(app.state.db, _user_id, reserved_plan, pipeline_response)

    return Response(
        content=pipeline_response.content,
        status_code=pipeline_response.status_code,
        media_type=pipeline_response.headers.get("content-type", "application/json"),
    )


@app.get("/account/me", response_model=AccountResponse)
async def account_me(user_id: str = Depends(current_user_id)) -> AccountResponse:
    row, used = await _account(app.state.db, user_id)
    plan = PlanName(row["plan"])
    features = PLAN_FEATURES[plan]
    remaining = None
    if plan == PlanName.STUDENT:
        remaining = max(settings.student_monthly_report_limit - used, 0)
    elif plan == PlanName.BASIC:
        remaining = int(row["report_credits"])
    return AccountResponse(
        id=row["id"],
        username=row["username"],
        email=row["email"],
        plan=plan,
        email_verified=row["email_verified"],
        reports_used_this_month=used,
        reports_remaining=remaining,
        pdf_export_enabled=features["pdf"],
        chatbot_enabled=features["chatbot"],
        external_api_enabled=features["api"],
        payment_required=features["payment"],
    )


@app.post("/billing/mock/checkout", response_model=PaymentCheckoutResponse, status_code=201)
async def mock_checkout(
    body: PaymentCheckoutRequest,
    user_id: str = Depends(current_user_id),
) -> PaymentCheckoutResponse:
    if not settings.mock_payments_enabled:
        raise HTTPException(status_code=404, detail="Mock payments are disabled")
    if body.plan == PlanName.STUDENT:
        raise HTTPException(status_code=400, detail="Student Plan does not require payment")

    checkout_id = str(uuid4())
    async with app.state.db.acquire() as connection:
        account = await connection.fetchrow(
            "SELECT id FROM users WHERE id=$1::uuid AND is_active=TRUE",
            user_id,
        )
        if account is None:
            raise HTTPException(status_code=401, detail="User account is unavailable")
        await connection.execute(
            "INSERT INTO mock_payment_transactions "
            "(checkout_id, user_id, plan, amount) VALUES ($1::uuid, $2::uuid, $3, $4)",
            checkout_id,
            user_id,
            body.plan.value,
            MOCK_PLAN_PRICES[body.plan],
        )
    return PaymentCheckoutResponse(
        checkout_id=checkout_id,
        plan=body.plan,
        amount=MOCK_PLAN_PRICES[body.plan],
    )


@app.post("/billing/mock/confirm", response_model=MockPaymentConfirmResponse)
async def confirm_mock_payment(
    body: MockPaymentConfirmRequest,
    user_id: str = Depends(current_user_id),
) -> MockPaymentConfirmResponse:
    if not settings.mock_payments_enabled:
        raise HTTPException(status_code=404, detail="Mock payments are disabled")
    try:
        checkout_uuid = UUID(body.checkout_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid checkout_id")

    async with app.state.db.acquire() as connection:
        async with connection.transaction():
            transaction = await connection.fetchrow(
                "SELECT checkout_id::text, plan, status FROM mock_payment_transactions "
                "WHERE checkout_id=$1::uuid AND user_id=$2::uuid FOR UPDATE",
                checkout_uuid,
                user_id,
            )
            if transaction is None:
                raise HTTPException(status_code=404, detail="Checkout session not found")
            plan = PlanName(transaction["plan"])
            if transaction["status"] == "completed":
                return MockPaymentConfirmResponse(
                    checkout_id=transaction["checkout_id"],
                    plan=plan,
                    status="completed",
                    message="Payment was already confirmed",
                )

            if plan == PlanName.BASIC:
                await connection.execute(
                    "UPDATE users SET plan=$1, report_credits=5, subscription_status='inactive' "
                    "WHERE id=$2::uuid",
                    plan.value,
                    user_id,
                )
            else:
                await connection.execute(
                    "UPDATE users SET plan=$1, report_credits=0, subscription_status='active' "
                    "WHERE id=$2::uuid",
                    plan.value,
                    user_id,
                )
            await connection.execute(
                "UPDATE mock_payment_transactions SET status='completed', completed_at=now() "
                "WHERE checkout_id=$1::uuid",
                checkout_uuid,
            )

    return MockPaymentConfirmResponse(
        checkout_id=str(checkout_uuid),
        plan=plan,
        status="completed",
        message=f"Mock payment confirmed and {plan.value} plan activated",
    )
