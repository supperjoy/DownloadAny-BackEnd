from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "DownloadAny API"
    environment: str = "development"
    database_url: str = "mysql+pymysql://root@127.0.0.1:3306/downloadany?charset=utf8mb4"
    jwt_secret: str = "development-only-change-this-secret"
    frontend_url: str = "http://localhost:3000"
    google_client_id: str = ""

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@example.com"
    smtp_starttls: bool = True

    trust_proxy_headers: bool = False
    access_token_minutes: int = 30
    refresh_token_days: int = 30
    action_token_minutes: int = 15
    max_login_failures: int = 5
    login_window_minutes: int = 15
    account_lock_minutes: int = 15
    captcha_after_failures: int = 3
    max_ip_login_failures: int = 20
    email_cooldown_seconds: int = 60
    max_emails_per_hour: int = 5
    max_ip_emails_per_hour: int = 20

    @field_validator("jwt_secret")
    @classmethod
    def validate_secret(cls, value: str, info):
        if len(value) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 characters")
        return value

    @model_validator(mode="after")
    def reject_development_secret_in_production(self):
        if self.environment == "production" and self.jwt_secret == "development-only-change-this-secret":
            raise ValueError("JWT_SECRET must be changed in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
