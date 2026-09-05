from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="IPR_", extra="ignore"
    )

    # Read from MONGODB_URI too, so the value can live under the name your other
    # services already use.
    mongodb_uri: str = ""
    mongodb_db: str = ""  # blank -> database named in the URI path

    collection_applications: str = "applications"
    collection_snapshots: str = "status_snapshots"
    collection_captcha: str = "captcha_challenges"
    collection_runs: str = "scrape_runs"
    collection_agent_runs: str = "agent_runs"

    min_request_interval: float = 1.5
    http_timeout: float = 45.0
    max_captcha_attempts: int = 4
    max_target_attempts: int = 5

    operator_host: str = "127.0.0.1"
    operator_port: int = 8765
    captcha_wait_timeout: float = 180.0

    save_raw_html: bool = True
    raw_html_dir: Path = Path("data/raw")

    # Public Waypoint browser-agent limits. These keep anonymous runs bounded;
    # raise them only after adding account-level quotas or billing.
    public_runs_per_hour: int = 6
    agent_max_steps: int = 30
    agent_max_concurrency: int = 2
    agent_session_timeout: int = 900
    agent_model: str = "gpt-4o"
    cors_origins: str = "http://localhost:3000"

    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )


class _MongoUriFallback(BaseSettings):
    """Picks up a bare MONGODB_URI (no IPR_ prefix)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    mongodb_uri: str = ""


settings = Settings()
if not settings.mongodb_uri:
    settings.mongodb_uri = _MongoUriFallback().mongodb_uri
