"""App configuration, loaded from environment variables / .env."""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Postgres
    postgres_host: str = "db"
    postgres_port: int = 5432
    postgres_db: str = "trackfox"
    # The app connects as a low-privilege role, never the superuser. The env
    # names are APP_DB_* so they can't be confused with POSTGRES_USER /
    # POSTGRES_PASSWORD, which the postgres image uses for its superuser.
    postgres_user: str = Field(default="trackfox_app", validation_alias="APP_DB_USER")
    # No default: refuse to start rather than connect with a placeholder.
    postgres_password: str = Field(validation_alias="APP_DB_PASSWORD")

    # App behavior
    app_env: str = "development"
    poll_interval_ms: int = 60000


settings = Settings()
