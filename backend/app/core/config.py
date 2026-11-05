from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_PLACEHOLDER_VALUES = {
    "changeme",
    "password",
    "replace_me",
    "replace_with_a_long_random_secret",
    "replace_with_a_secure_random_value",
    "replace_with_database_password",
    "secret",
    "your_mysql_password",
}


class Settings(BaseSettings):
    """Application settings loaded from environment variables or a local .env file."""

    app_name: str = "Smart OA Agent"
    app_env: Literal["development", "test", "production"] = "development"
    debug: bool = False
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"

    mysql_host: str = "localhost"
    mysql_port: int = Field(default=3306, ge=1, le=65535)
    mysql_user: str = ""
    mysql_password: SecretStr = SecretStr("")
    mysql_database: str = "smart_oa"

    redis_host: str = "localhost"
    redis_port: int = Field(default=6379, ge=1, le=65535)
    redis_db: int = Field(default=0, ge=0)
    redis_password: SecretStr = SecretStr("")
    redis_key_prefix: str = "smart_oa"
    session_ttl_seconds: int = Field(default=2592000, ge=0)

    jwt_secret_key: SecretStr = SecretStr("")
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = Field(default=30, gt=0)

    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        validate_default=True,
    )

    @field_validator("app_env", mode="before")
    @classmethod
    def normalize_app_env(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @property
    def cors_origin_list(self) -> list[str]:
        """Return the configured comma-separated browser origins."""
        return [
            origin.strip()
            for origin in self.cors_origins.split(",")
            if origin.strip()
        ]

    @model_validator(mode="after")
    def validate_required_configuration(self) -> "Settings":
        missing = []
        required_text = {
            "MYSQL_HOST": self.mysql_host,
            "MYSQL_USER": self.mysql_user,
            "MYSQL_DATABASE": self.mysql_database,
            "JWT_SECRET_KEY": self.jwt_secret_key.get_secret_value(),
        }
        for name, value in required_text.items():
            if not value.strip():
                missing.append(name)
        if missing:
            raise ValueError(
                "Missing required configuration: " + ", ".join(missing)
            )

        if self.app_env == "production":
            jwt_secret = self.jwt_secret_key.get_secret_value().strip()
            database_password = self.mysql_password.get_secret_value().strip()
            if len(jwt_secret) < 32 or jwt_secret.lower() in _PLACEHOLDER_VALUES:
                raise ValueError(
                    "JWT_SECRET_KEY must be a non-default random value of at least "
                    "32 characters in production"
                )
            if (
                not database_password
                or database_password.lower() in _PLACEHOLDER_VALUES
            ):
                raise ValueError(
                    "MYSQL_PASSWORD must be configured with a non-default value "
                    "in production"
                )

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
