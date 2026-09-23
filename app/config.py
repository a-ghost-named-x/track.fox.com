"""App configuration, loaded from environment variables / .env."""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Postgres (manual-entry DB)
    postgres_host: str = "db"
    postgres_port: int = 5432
    postgres_db: str = "trackfox"
    # The app connects as a low-privilege role, never the superuser. The env
    # names are APP_DB_* so they can't be confused with POSTGRES_USER /
    # POSTGRES_PASSWORD, which the postgres image uses for its superuser.
    postgres_user: str = Field(default="trackfox_app", validation_alias="APP_DB_USER")
    # No default: refuse to start rather than connect with a placeholder.
    postgres_password: str = Field(validation_alias="APP_DB_PASSWORD")

    # MSSQL (existing production server, read-only). Not required yet because
    # nothing queries it (see the TODO in dashboard.py).
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
    def mssql_connection_string(self) -> str:
        return (
            f"DRIVER={{{self.mssql_driver}}};"
            f"SERVER={self.mssql_host},{self.mssql_port};"
            f"DATABASE={self.mssql_db};"
            f"UID={self.mssql_user};PWD={self.mssql_password};"
            "Encrypt=yes;TrustServerCertificate=yes;"
        )


settings = Settings()
