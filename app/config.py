from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR.parent / "runtime"
DOWNLOAD_DIR = RUNTIME_DIR / "downloads"
DB_PATH = RUNTIME_DIR / "jobs.sqlite3"


class Settings(BaseSettings):
    app_name: str = "Bookchoco Download"
    app_env: str = "production"
    public_base_url: str = "https://api-download.bookchoco.online"
    frontend_origin: str = "https://download.bookchoco.online"
    max_video_mb: int = 350
    job_ttl_minutes: int = 45
    rate_limit_requests: int = 8
    rate_limit_window_seconds: int = 60
    max_concurrent_downloads: int = 1
    download_max_height: int = 1080
    extra_allowed_hosts: str = ""
    youtube_proxy_url: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def max_video_bytes(self) -> int:
        return self.max_video_mb * 1024 * 1024

    @property
    def extra_hosts(self) -> set[str]:
        return {h.strip().lower().rstrip(".") for h in self.extra_allowed_hosts.split(",") if h.strip()}


settings = Settings()
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
