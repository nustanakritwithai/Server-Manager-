from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Environment variables use the SIMCORE_ prefix."""

    model_config = SettingsConfigDict(env_prefix="SIMCORE_", extra="ignore")

    database_url: str = "postgresql+psycopg://simcore:simcore@127.0.0.1:5432/simcore"
    env: str = "development"
    admin_token: str = "dev-admin"
    enable_admin: bool = False
    worker_poll_seconds: float = 1.0
    max_event_attempts: int = 5

    @property
    def admin_enabled(self) -> bool:
        if self.env == "production":
            return self.enable_admin
        return True


@lru_cache
def get_settings() -> Settings:
    return Settings()
