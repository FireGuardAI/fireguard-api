from enum import StrEnum

from pydantic import BaseModel, Field


class PlanName(StrEnum):
    STUDENT = "student"
    BASIC = "basic"
    PRO = "pro"
    ENTERPRISE = "enterprise"
    GOVERNMENT = "government"


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=120, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=12, max_length=256)
    email: str | None = Field(default=None, max_length=254)
    plan: PlanName = PlanName.STUDENT


class UserResponse(BaseModel):
    id: str
    username: str
    plan: PlanName = PlanName.STUDENT
    email_verified: bool = False
    payment_required: bool = False
    checkout_id: str | None = None
    amount: int | None = None
    currency: str = "LKR"


class AccountResponse(BaseModel):
    id: str
    username: str
    email: str | None
    plan: PlanName
    email_verified: bool
    reports_used_this_month: int
    reports_remaining: int | None
    pdf_export_enabled: bool
    chatbot_enabled: bool
    external_api_enabled: bool
    payment_required: bool


class PaymentCheckoutRequest(BaseModel):
    plan: PlanName


class PaymentCheckoutResponse(BaseModel):
    checkout_id: str
    plan: PlanName
    amount: int
    currency: str = "LKR"
    status: str = "pending"


class MockPaymentConfirmRequest(BaseModel):
    checkout_id: str = Field(min_length=1, max_length=100)


class MockPaymentConfirmResponse(BaseModel):
    checkout_id: str
    plan: PlanName
    status: str
    message: str


class TextAnalyzeRequest(BaseModel):
    raw_prompt: str = Field(min_length=1, max_length=60_000)
    building_name: str | None = Field(default=None, max_length=200)
    auditor_notes: str | None = Field(default=None, max_length=4000)
