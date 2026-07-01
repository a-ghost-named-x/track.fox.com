"""Centralized app configuration, loaded from environment variables / .env."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Postgres (manual-entry DB)
    postgres_host: str = "db"
    postgres_port: int = 5432
    postgres_db: str = "trackfox"
    postgres_user: str = "trackfox_app"
    # No default — the app should refuse to start rather than silently
    # connect with a placeholder password if .env is missing or misconfigured.
    postgres_password: str

    # MSSQL (existing production server, read-only)
    # Left with defaults, not required, since MSSQL querying isn't wired into
    # the app yet (see TODO in dashboard.py) — nothing reads these values in
    # production today, so they shouldn't be able to block app startup.
    # Revisit making these required once the MSSQL read query is implemented.
    mssql_host: str = "your-mssql-host"
    mssql_port: int = 1433
    mssql_db: str = "ProductionDB"
    mssql_user: str = "readonly_app_user"
    mssql_password: str = "changeme"
    mssql_driver: str = "ODBC Driver 18 for SQL Server"

    # App behavior
    app_env: str = "development"
    poll_interval_ms: int = 60000

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def mssql_connection_string(self) -> str:
        return (
            f"DRIVER={{{self.mssql_driver}}};"
            f"SERVER={self.mssql_host},{self.mssql_port};"
            f"DATABASE={self.mssql_db};"
            f"UID={self.mssql_user};PWD={self.mssql_password};"
            "Encrypt=yes;TrustServerCertificate=yes;"
        )


settings = Settings()
