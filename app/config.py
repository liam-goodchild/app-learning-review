from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    app_host: str
    app_port: int
    database_url: str
    vault_path: Path
    vault_import_folders: list[str]
    app_secret_key: str
    app_username: str
    app_password: str
    app_password_hash: str | None
    session_cookie_name: str
    secure_cookies: bool
    scan_interval_seconds: int
    min_file_age_seconds: int
    review_session_size: int
    enable_periodic_scan: bool
    worker_token: str | None = None


def get_settings() -> Settings:
    load_dotenv()
    return Settings(
        app_host=os.getenv("APP_HOST", "0.0.0.0"),
        app_port=int(os.getenv("APP_PORT", "8080")),
        database_url=os.getenv("DATABASE_URL", "sqlite:///./data/learning.db"),
        vault_path=Path(os.getenv("VAULT_PATH", "/vault")),
        vault_import_folders=_csv(os.getenv("VAULT_IMPORT_FOLDERS", "00 - Inbox,02 - Notes")),
        app_secret_key=os.getenv("APP_SECRET_KEY", "change-me"),
        app_username=os.getenv("APP_USERNAME", "admin"),
        app_password=os.getenv("APP_PASSWORD", "change-me"),
        app_password_hash=os.getenv("APP_PASSWORD_HASH") or None,
        session_cookie_name=os.getenv("SESSION_COOKIE_NAME", "learning_session"),
        secure_cookies=os.getenv("SECURE_COOKIES", "false").lower() in {"1", "true", "yes", "on"},
        scan_interval_seconds=int(os.getenv("SCAN_INTERVAL_SECONDS", "300")),
        min_file_age_seconds=int(os.getenv("MIN_FILE_AGE_SECONDS", "20")),
        review_session_size=int(os.getenv("REVIEW_SESSION_SIZE", "12")),
        enable_periodic_scan=os.getenv("ENABLE_PERIODIC_SCAN", "true").lower() in {"1", "true", "yes", "on"},
        worker_token=os.getenv("WORKER_TOKEN") or None,
    )
