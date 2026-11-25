"""Settings loaded from environment variables / a local .env file."""

from pathlib import Path
from urllib.parse import urlparse

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class MissingSettingError(RuntimeError):
    pass


class Settings(BaseSettings):
    # env_ignore_empty: a blank `NAME=` line in .env counts as "not set" (so blank ints don't crash).
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    database_url: SecretStr | None = None

    openpanel_client_id: SecretStr | None = None
    openpanel_client_secret: SecretStr | None = None
    openpanel_project_id: str | None = None
    openpanel_api_url: str = "https://api.openpanel.dev"

    site_url: str = "https://ssharp.app"

    resend_api_key: SecretStr | None = None
    alert_from_email: str | None = None
    alert_recipients: str | None = None

    # Test/team exclusions (defaults; the dashboard can override them), and where state is stored.
    test_email_domains: str = "fake.com,habit10x.internal,example.com"
    team_emails: str | None = None
    traction_db_path: str = "data/traction.db"
    dashboard_password: SecretStr | None = None

    # Social media tracker (short-video views / likes / comments).
    social_input_dir: str = "input"
    youtube_api_key: SecretStr | None = None  # optional: more reliable than scraping, and works from a server
    social_proxy: str | None = None  # optional proxy used for TikTok only (it is blocked in India)
    social_cookies_file: str | None = None  # optional Netscape cookies.txt for Instagram / X logins

    # Optional fallback when there is no Resend/domain yet (e.g. Gmail + app password).
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None

    @field_validator("openpanel_project_id")
    @classmethod
    def _project_id_from_url(cls, value: str | None) -> str | None:
        """Accept a pasted dashboard URL (.../<organization>/<projectId>) and keep just the project id."""
        if value and "/" in value:
            segments = [s for s in urlparse(value.strip()).path.split("/") if s]
            return segments[-1] if segments else None
        return value.strip() if value else value

    def require(self, *names: str) -> None:
        """Raise one clear error listing every required setting that is empty."""
        missing = []
        for name in names:
            value = getattr(self, name)
            raw = value.get_secret_value() if isinstance(value, SecretStr) else value
            if not raw:
                missing.append(name.upper())
        if missing:
            raise MissingSettingError(f"Missing in .env: {', '.join(missing)}")

    @property
    def state_path(self) -> Path:
        """Absolute path of the state file. A relative TRACTION_DB_PATH is anchored to the project folder, so the
        dashboard and the checker always share one file no matter which folder they were started from."""
        path = Path(self.traction_db_path)
        return path if path.is_absolute() or str(path) == ":memory:" else PROJECT_ROOT / path

    @property
    def input_path(self) -> Path:
        """Folder holding the Excel files of video links (a relative path is anchored to the project folder)."""
        path = Path(self.social_input_dir)
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def recipients(self) -> list[str]:
        return [r.strip() for r in (self.alert_recipients or "").split(",") if r.strip()]


def get_settings() -> Settings:
    return Settings()
