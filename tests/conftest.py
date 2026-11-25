import pytest

ENV_NAMES = [
    "DATABASE_URL", "OPENPANEL_CLIENT_ID", "OPENPANEL_CLIENT_SECRET", "OPENPANEL_PROJECT_ID", "OPENPANEL_API_URL",
    "ALERT_RECIPIENTS", "RESEND_API_KEY", "ALERT_FROM_EMAIL", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD",
    "SITE_URL", "TEST_EMAIL_DOMAINS", "TEAM_EMAILS", "TRACTION_DB_PATH", "DASHBOARD_PASSWORD",
    "SOCIAL_INPUT_DIR", "YOUTUBE_API_KEY", "SOCIAL_PROXY", "SOCIAL_COOKIES_FILE",
]


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Never read the developer's real .env, environment or state file during tests."""
    monkeypatch.chdir(tmp_path)
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRACTION_DB_PATH", str(tmp_path / "state.db"))
