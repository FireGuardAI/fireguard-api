from pydantic import BaseModel, Field


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=120, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=12, max_length=256)


class UserResponse(BaseModel):
    id: str
    username: str


class TextAnalyzeRequest(BaseModel):
    raw_prompt: str = Field(min_length=1, max_length=60_000)
    building_name: str | None = Field(default=None, max_length=200)
    auditor_notes: str | None = Field(default=None, max_length=4000)
