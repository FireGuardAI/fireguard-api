from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    api_title: str = "FireGuard API Gateway"
    api_version: str = "1.0.0"
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    database_url: str = "postgresql://fireguard:fireguard@postgres:5432/fireguard"
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = Field(default=60, gt=0)
    internal_api_key: str
    intake_service_url: str = "http://agent-intake:8004"
    retrieval_service_url: str = "http://agent-retrieval:8001"
    request_timeout_seconds: float = Field(default=330.0, gt=0)
    max_upload_bytes: int = Field(default=15 * 1024 * 1024, gt=0)
    analyze_rate_limit: str = "10/minute"
    student_monthly_report_limit: int = Field(default=5, ge=1, le=5)
    institutional_email_domains: list[str] = Field(
        default_factory=lambda: ["sliit.lk", "uom.lk"]
    )
    mock_payments_enabled: bool = False


settings = Settings()
