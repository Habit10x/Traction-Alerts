from datetime import datetime, timedelta, timezone
from pathlib import Path

from habit10x_marketing_automation.config import Settings
from habit10x_marketing_automation.emails import plain, render_alert
from habit10x_marketing_automation.engine import Firing
from habit10x_marketing_automation.rules import RULES, Event
from habit10x_marketing_automation.store import Store

UTC = timezone.utc
T0 = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
SETTINGS = Settings(site_url="https://site.test")


# ------------------------------------------------------------------------------------- store
def test_seed_never_overwrites_edited_rules():
    store = Store(":memory:")
    defaults = {r.id: r.default_config() for r in RULES.values()}
    store.seed_rules(defaults)
    edited = store.get_rules()["challenge_visitors"]
    edited.threshold = 9
    store.save_rule(edited)
    store.seed_rules(defaults)
    assert store.get_rules()["challenge_visitors"].threshold == 9


def test_settings_roundtrip_and_alert_queries():
    store = Store(":memory:")
    assert store.get_setting("x", "fallback") == "fallback"
    store.set_setting("x", "1")
    assert store.get_setting("x") == "1"
    for status in ("sent", "failed", "recorded"):
        store.add_alert(fired_at=T0, rule_id="r", entity="e", count=1, threshold=1, window_minutes=60,
                        status=status, subject=status)
    assert store.count_sent_since(T0 - timedelta(hours=1)) == 1
    assert [a["status"] for a in store.alerts(statuses=["failed"])] == ["failed"]
    assert "e" in store.last_fired("r")  # sent/recorded start the cooldown, failed does not


def test_failed_alerts_do_not_start_a_cooldown():
    store = Store(":memory:")
    store.add_alert(fired_at=T0, rule_id="r", entity="e", count=1, threshold=1, window_minutes=60,
                    status="failed", subject="s")
    assert store.last_fired("r") == {}


# ------------------------------------------------------------------------------------ emails
def firing(rule_id: str, infos: list[dict], count: int | None = None) -> Firing:
    events = [Event(T0 + timedelta(seconds=i), info.get("slug", ""), str(i), info) for i, info in enumerate(infos)]
    return Firing(rule_id, "", count or len(events), events, events[-1].ts)


def render(rule_id: str, infos: list[dict], count: int | None = None):
    rule = RULES[rule_id]
    return render_alert(rule, rule.default_config(), firing(rule_id, infos, count), SETTINGS)


def test_titles_are_stripped_and_escaped_and_the_threshold_is_stated():
    info = {"num": 40, "title": '<p>Is <b>arriving</b> early</p><script>alert(1)</script>', "response_type": "text",
            "wit_score": 80, "user_name": "<img src=x>Sam"}
    subject, html_body, text = render("outside_attempt", [info])
    assert subject.startswith('Outside visitor tried Challenge #40 "Is arriving early')
    assert "<script>" not in html_body and "<img" not in html_body
    assert "threshold: 1 or more" in text and "https://site.test/challenge/40" in html_body


def test_each_first_release_rule_renders_a_specific_subject():
    cases = {
        "new_signup": ({"name": "Asha", "email": "a@x.com", "platform": "instagram", "slug": "ig-humor"}, "New signup: Asha"),
        "plan_started": ({"name": "Ravi", "email": "r@x.com", "plan": "pro", "status": "active"}, "Plan started: Ravi (pro)"),
        "great_response": ({"num": 3, "title": "T", "wit_score": 94, "response_type": "audio", "user_name": "Z"},
                           "Great response (score 94)"),
        "source_signup": ({"name": "Asha", "email": "a@x.com", "platform": "reddit", "slug": "re-slug"}, "Signup from reddit (re-slug)"),
        "pricing_interest": ({"path": "/pricing", "country": "IN"}, "Interested in paying"),
    }
    for rule_id, (info, expected) in cases.items():
        assert expected in render(rule_id, [info])[0], rule_id


def test_spike_rules_name_the_count_and_window():
    info = {"num": 38, "title": "Longer route"}
    assert render("challenge_visitors", [info] * 5, count=5)[0] == (
        'Challenge #38 "Longer route" is catching on: 5 visitors in the last 60 min')
    assert "3 people attempting" in render("attempts_surge", [info] * 3, count=3)[0]
    assert render("site_visitors", [{"path": "/home"}] * 12, count=12)[0] == "Site-wide surge: 12 visitors in the last 60 min"
    assert "from instagram (ig-humor)" in render("source_visitors", [{"platform": "instagram", "slug": "ig-humor"}])[0]


def test_system_error_email_explains_the_failure():
    subject, _, text = render("system_error", [{"source": "neon", "error": "connection refused"}], count=3)
    assert subject == "System cannot read neon data (3 checks in a row)" and "connection refused" in text


def test_plain_strips_markup():
    assert plain("<p>A&amp;B   <i>c</i></p>") == "A&B c"


# --------------------------------------------------------------------------------- dashboard
DASHBOARD = Path(__file__).resolve().parents[1] / "src" / "habit10x_marketing_automation" / "dashboard.py"


def open_app(view: str = "Overview"):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(DASHBOARD), default_timeout=60).run()
    if view != "Overview":
        at.radio(key="view").set_value(view).run()
    return at


def click(at, label: str):
    next(b for b in at.button if b.label == label).click().run()


def test_dashboard_shows_every_view_without_credentials_and_reports_problems_instead_of_crashing():
    at = open_app()
    assert not at.exception, at.exception
    assert [m.label for m in at.metric][:2] == ["Last check", "Data sources"]
    for view in ("Alert thresholds", "History", "Settings"):
        at.radio(key="view").set_value(view).run()
        assert not at.exception, (view, at.exception)
    at.radio(key="view").set_value("Alert thresholds").run()
    assert any("Could not read data" in e.value for e in at.error)  # no DATABASE_URL: explained, not a crash


def test_threshold_screen_has_window_and_cooldown_per_group_and_a_threshold_per_alert():
    at = open_app("Alert thresholds")
    assert [h.value for h in at.subheader] == ["Activity alerts", "Traffic and attention alerts", "System health"]
    assert (at.number_input(key="activity_wi").value, at.number_input(key="activity_cd").value) == (60, 15)
    assert (at.number_input(key="traffic_wi").value, at.number_input(key="traffic_cd").value) == (60, 60)
    for rule_id, rule in RULES.items():
        assert at.number_input(key=f"{rule_id}_th").value == rule.threshold
        assert at.toggle(key=f"{rule_id}_en").value is True
    assert {at.number_input(key=f"{r}_th").value for r in
            ("source_visitors", "challenge_visitors", "attempts_surge", "site_visitors")} == {1, 5, 3, 10}
    page = " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)
    assert "Suggested" not in page and "What to expect" not in page and "minimum score" not in page.lower()


def test_each_alert_saves_its_own_threshold_and_the_group_shares_window_and_cooldown():
    from habit10x_marketing_automation.engine import load_configs

    at = open_app("Alert thresholds")
    assert at.button(key="traffic_save").disabled  # nothing changed yet
    at.number_input(key="challenge_visitors_th").set_value(8).run()
    at.number_input(key="traffic_wi").set_value(30).run()
    at.toggle(key="site_visitors_en").set_value(False).run()
    at.button(key="traffic_save").click().run()
    assert not at.exception, at.exception
    configs = load_configs(Store(Settings().state_path), RULES)
    assert configs["challenge_visitors"].threshold == 8
    assert (configs["attempts_surge"].threshold, configs["site_visitors"].threshold) == (3, 10)  # own values kept
    for rule_id in ("source_visitors", "challenge_visitors", "attempts_surge", "site_visitors"):
        assert configs[rule_id].window_minutes == 30  # shared by the group
    assert configs["site_visitors"].enabled is False and configs["attempts_surge"].enabled is True
    assert configs["new_signup"].window_minutes == 60  # the other group is untouched
    assert any("saved" in s.value for s in at.success)


def test_email_switch_and_check_interval_are_saved():
    at = open_app()
    at.toggle(key="email_switch").set_value(True).run()
    at.radio(key="view").set_value("Settings").run()
    next(s for s in at.selectbox if "checker looks" in s.label).set_value(10).run()
    click(at, "Save settings")
    assert not at.exception, at.exception
    store = Store(Settings().state_path)
    assert store.get_setting("email_enabled") == "1" and store.get_setting("check_interval_minutes") == "10"


def test_dashboard_password_gate_blocks_until_correct(monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    at = AppTest.from_file(str(DASHBOARD), default_timeout=60).run()
    assert len(at.radio) == 0  # nothing is shown before the password
    at.text_input[0].set_value("wrong").run()
    assert at.error and len(at.radio) == 0
    at.text_input[0].set_value("s3cret").run()
    assert len(at.radio) == 1


# ------------------------------------------------------------------------------ OpenPanel paging
class FakeOpenPanel:
    """Serves events newest-first, 2 per page, like the real API."""

    def __init__(self, timestamps: list[datetime]):
        self.events = [{"id": f"e{i}", "name": "screen_view", "createdAt": ts.isoformat().replace("+00:00", "Z"),
                        "deviceId": f"d{i}", "properties": {}} for i, ts in enumerate(sorted(timestamps, reverse=True))]
        self.pages_requested = []

    def export_events(self, event, start, end, page, limit, includes):
        self.pages_requested.append(page)
        chunk = self.events[(page - 1) * 2: page * 2]
        return {"meta": {"pages": -(-len(self.events) // 2)}, "data": chunk}


def test_openpanel_paging_stops_once_events_are_older_than_the_window():
    from habit10x_marketing_automation.rules import Context, _op_events

    stamps = [T0 - timedelta(minutes=m) for m in (1, 2, 5, 90, 120, 180, 240, 300)]
    fake = FakeOpenPanel(stamps)
    ctx = Context(SETTINGS, [], [], openpanel_factory=lambda: fake)
    got = _op_events(ctx, "screen_view", T0 - timedelta(minutes=60), T0)
    assert len(got) == 3  # only the three events inside the last hour
    assert fake.pages_requested == [1, 2]  # page 3+ (older events) was never requested
