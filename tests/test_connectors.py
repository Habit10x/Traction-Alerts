import json

import httpx
import pytest

from habit10x_marketing_automation.config import MissingSettingError, Settings
from habit10x_marketing_automation.mailer import MailError, send_email
from habit10x_marketing_automation.openpanel import OpenPanel, OpenPanelError

def make_settings(**overrides) -> Settings:
    values = dict(
        openpanel_client_id="cid", openpanel_client_secret="secret", openpanel_project_id="proj1",
    )
    return Settings(**{**values, **overrides})


def make_client(handler) -> OpenPanel:
    return OpenPanel(make_settings(), transport=httpx.MockTransport(handler))


def test_require_lists_every_missing_setting():
    with pytest.raises(MissingSettingError) as exc:
        Settings().require("database_url", "openpanel_client_id")
    assert "DATABASE_URL" in str(exc.value) and "OPENPANEL_CLIENT_ID" in str(exc.value)


@pytest.mark.parametrize(
    "raw",
    ["habit10x", " habit10x ", "https://dashboard.openpanel.dev/habit10x-workspace/habit10x",
     "https://dashboard.openpanel.dev/habit10x-workspace/habit10x/"],
)
def test_project_id_accepts_a_pasted_dashboard_url(raw):
    assert Settings(openpanel_project_id=raw).openpanel_project_id == "habit10x"


def test_recipients_are_split_and_trimmed():
    assert Settings(alert_recipients=" a@x.com, b@x.com ,,").recipients == ["a@x.com", "b@x.com"]


def test_openpanel_sends_auth_headers_and_project_path():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"], seen["url"] = request.headers, request.url
        return httpx.Response(200, json={"visitors": 3})

    with make_client(handler) as op:
        assert op.insights("live") == {"visitors": 3}
    assert seen["headers"]["openpanel-client-id"] == "cid"
    assert seen["headers"]["openpanel-client-secret"] == "secret"
    assert seen["url"].path == "/insights/proj1/live"


def test_export_events_sends_project_and_repeated_includes_and_drops_none():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = request.url
        return httpx.Response(200, json=[])

    with make_client(handler) as op:
        op.export_events(start="2026-11-01", limit=None, includes=["profile", "properties"])
    query = seen["url"].params
    assert query["projectId"] == "proj1" and query["start"] == "2026-11-01"
    assert query.get_list("includes") == ["profile", "properties"]
    assert "limit" not in query


@pytest.mark.parametrize("status", [401, 403])
def test_openpanel_auth_failure_explains_client_type(status):
    with make_client(lambda r: httpx.Response(status)) as op:
        with pytest.raises(OpenPanelError, match="'read' or 'root'"):
            op.insights("live")


def test_openpanel_requires_credentials():
    with pytest.raises(MissingSettingError, match="OPENPANEL_CLIENT_SECRET"):
        OpenPanel(Settings(openpanel_client_id="cid", openpanel_project_id="p"))


def test_project_id_needed_only_for_project_scoped_calls():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "p1"}])

    settings = make_settings(openpanel_project_id=None)
    with OpenPanel(settings, transport=httpx.MockTransport(handler)) as op:
        assert op.get("/manage/projects") == [{"id": "p1"}]
        with pytest.raises(MissingSettingError, match="OPENPANEL_PROJECT_ID"):
            op.insights("live")


def test_send_email_posts_expected_payload_to_resend():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"], seen["url"], seen["body"] = request.headers["authorization"], request.url, json.loads(request.content)
        return httpx.Response(200, json={"id": "msg_1"})

    settings = Settings(resend_api_key="re_key", alert_from_email="Alerts <a@x.com>", alert_recipients="o@x.com, t@x.com")
    message_id = send_email(settings, "Subj", "<p>hi</p>", "hi", transport=httpx.MockTransport(handler))
    assert message_id == "msg_1"
    assert seen["auth"] == "Bearer re_key" and seen["url"].path == "/emails"
    assert seen["body"]["to"] == ["o@x.com", "t@x.com"] and seen["body"]["from"] == "Alerts <a@x.com>"


def test_send_email_surfaces_resend_errors():
    settings = Settings(resend_api_key="k", alert_from_email="a@x.com", alert_recipients="o@x.com")
    transport = httpx.MockTransport(lambda r: httpx.Response(403, json={"message": "domain not verified"}))
    with pytest.raises(MailError, match="domain not verified"):
        send_email(settings, "s", "h", "t", transport=transport)


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.tls, self.login_args, self.sent = host, port, False, None, None
        FakeSMTP.instances.append(self)

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, message):
        self.sent = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_send_email_falls_back_to_smtp_without_resend(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr("habit10x_marketing_automation.mailer.smtplib.SMTP", FakeSMTP)
    settings = Settings(
        alert_from_email="me@gmail.com", alert_recipients="o@x.com, t@x.com",
        smtp_host="smtp.gmail.com", smtp_user="me@gmail.com", smtp_password="apppass",
    )
    send_email(settings, "Subj", "<p>hi</p>", "hi")
    server = FakeSMTP.instances[0]
    assert (server.host, server.port, server.tls) == ("smtp.gmail.com", 587, True)
    assert server.login_args == ("me@gmail.com", "apppass")
    assert server.sent["To"] == "o@x.com, t@x.com" and server.sent["Subject"] == "Subj"
    assert server.sent.get_body(("plain",)).get_content().strip() == "hi"


def test_send_email_with_no_provider_says_what_to_set():
    settings = Settings(alert_from_email="a@x.com", alert_recipients="o@x.com")
    with pytest.raises(MissingSettingError, match="RESEND_API_KEY"):
        send_email(settings, "s", "h", "t")


def test_blank_env_values_count_as_unset(tmp_path):
    (tmp_path / ".env").write_text("SMTP_PORT=\nSMTP_HOST=\nRESEND_API_KEY=\n", encoding="utf-8")
    settings = Settings()
    assert settings.smtp_port == 587 and settings.smtp_host is None


def test_send_email_needs_recipients():
    settings = Settings(resend_api_key="k", alert_from_email="a@x.com")
    with pytest.raises(MissingSettingError, match="ALERT_RECIPIENTS"):
        send_email(settings, "s", "h", "t")
