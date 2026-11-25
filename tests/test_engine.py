from datetime import datetime, timedelta, timezone

import pytest

from habit10x_marketing_automation.config import Settings
from habit10x_marketing_automation.engine import (RunResult, Runtime, effective_config, evaluate, live_counts,
                                                  load_configs, peak, rolling_counts, run_once, seed_defaults,
                                                  simulate)
from habit10x_marketing_automation.mailer import MailError
from habit10x_marketing_automation.rules import Context, Event, RuleDef
from habit10x_marketing_automation.store import GroupConfig, RuleConfig, Store

UTC = timezone.utc
T0 = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def make_rule(rule_id="r", **kw) -> RuleDef:
    base = dict(id=rule_id, name="Test rule", description="", source="neon", count_mode="events", per_entity=False,
                unit=("thing", "things"), collect=None)
    return RuleDef(**{**base, **kw})


def cfg(threshold=1, window=60, cooldown=0, rule_id="r") -> RuleConfig:
    return RuleConfig(rule_id, True, threshold, window, cooldown)


def ev(minutes_ago: float, actor="a", entity="") -> Event:
    return Event(T0 - timedelta(minutes=minutes_ago), entity, actor, {"num": 7, "title": "T"})


# ------------------------------------------------------------------------------------ evaluate
def test_fires_only_when_count_reaches_threshold():
    rule = make_rule()
    assert evaluate(rule, cfg(3), [ev(5, "a"), ev(4, "b")], T0) == []
    fired = evaluate(rule, cfg(3), [ev(5, "a"), ev(4, "b"), ev(3, "c")], T0)
    assert len(fired) == 1 and fired[0].count == 3
    assert evaluate(rule, cfg(3), [ev(i) for i in range(1, 6)], T0)[0].count == 5  # "or above" also fires


def test_events_outside_the_window_do_not_count():
    assert evaluate(make_rule(), cfg(1, window=10), [ev(30)], T0) == []
    assert len(evaluate(make_rule(), cfg(1, window=10), [ev(9)], T0)) == 1


def test_counting_resets_after_an_alert():
    rule, newest = make_rule(), ev(5)
    assert evaluate(rule, cfg(1), [newest], T0, watermarks={"": newest.ts}) == []  # already alerted
    again = evaluate(rule, cfg(1), [newest, ev(2, "b")], T0, watermarks={"": newest.ts})
    assert len(again) == 1 and again[0].count == 1  # only the newer event counts


def test_cooldown_holds_events_then_combines_them():
    rule = make_rule()
    last = {"": T0 - timedelta(minutes=5)}
    events = [ev(4, "a"), ev(2, "b")]
    assert evaluate(rule, cfg(1, cooldown=15), events, T0, last_fired=last) == []  # held, not lost
    after = evaluate(rule, cfg(1, cooldown=15), events, T0 + timedelta(minutes=11), last_fired=last)
    assert len(after) == 1 and after[0].count == 2  # one combined email


def test_actor_mode_counts_distinct_visitors_and_entities_are_separate():
    rule = make_rule(count_mode="actors", per_entity=True)
    events = [ev(5, "v1", "x"), ev(4, "v1", "x"), ev(3, "v1", "x"), ev(2, "v2", "y"), ev(1, "v3", "y")]
    fired = {f.entity: f.count for f in evaluate(rule, cfg(2), events, T0)}
    assert fired == {"y": 2}  # x has 3 events but only one visitor


def test_live_counts_ignore_alert_state_and_peak_finds_the_busiest_window():
    rule = make_rule()
    events = [ev(100), ev(99), ev(98), ev(5)]
    assert live_counts(rule, cfg(1, window=10), events, T0) == {"": 1}
    assert peak(rule, 10, events)[0] == 3
    assert peak(make_rule(count_mode="actors"), 10, [ev(5, "a"), ev(4, "a")])[0] == 1


def test_simulate_replays_the_checker_with_reset_and_cooldown():
    rule = make_rule()
    events = [Event(T0 - timedelta(hours=5) + timedelta(minutes=m), "", f"a{m}", {}) for m in (0, 1, 2, 120, 121)]
    fired = simulate(rule, cfg(2, window=60, cooldown=0), events, T0 - timedelta(hours=6), T0)
    assert [f.count for f in fired] == [3, 2]  # one email per burst, not one per check


# ------------------------------------------------------------------------------------- run_once
class Sender:
    def __init__(self, fail: bool = False):
        self.calls, self.fail = [], fail

    def __call__(self, settings, subject, html, text, to=None, **kw):
        if self.fail:
            raise MailError("535 bad credentials")
        self.calls.append({"subject": subject, "to": to})
        return "id"


@pytest.fixture
def env():
    settings = Settings(alert_recipients="owner@x.com", alert_from_email="a@x.com", site_url="https://site.test")
    store = Store(":memory:")
    box = {"events": [], "fail": False}

    def collect(ctx, since, until, params):
        if box["fail"]:
            raise RuntimeError("connection refused")
        return [e for e in box["events"] if since <= e.ts < until]

    rule = make_rule("outside_attempt", name="Outside visitor", collect=collect, unit=("attempt", "attempts"))
    return settings, store, box, {rule.id: rule}, Context(settings, [], [])


def go(env, now, sender, **kw) -> RunResult:
    settings, store, box, rules, ctx = env
    return run_once(settings, store, now=now, rules=rules, ctx=ctx, sender=sender, **kw)


def test_email_switch_off_records_but_does_not_send(env):
    settings, store, box, rules, ctx = env
    box["events"] = [ev(5)]
    sender = Sender()
    result = go(env, T0, sender)
    assert [r["status"] for r in result.fired] == ["recorded"] and sender.calls == []
    assert go(env, T0 + timedelta(minutes=5), sender).fired == []  # not re-announced when it is turned on later


def test_email_switch_on_sends_instantly_to_the_recipients(env):
    settings, store, box, rules, ctx = env
    store.set_setting("email_enabled", "1")
    box["events"] = [ev(5)]
    sender = Sender()
    result = go(env, T0, sender)
    assert result.fired[0]["status"] == "sent" and sender.calls[0]["to"] == ["owner@x.com"]
    assert "Outside" in sender.calls[0]["subject"] or "outside" in sender.calls[0]["subject"].lower()
    assert go(env, T0 + timedelta(minutes=5), sender).fired == []  # no repeat for the same event


def test_per_rule_recipients_override_the_default_list(env):
    settings, store, box, rules, ctx = env
    store.set_setting("email_enabled", "1")
    store.seed_rules({r.id: r.default_config() for r in rules.values()})
    saved = store.get_rules()["outside_attempt"]
    saved.recipients = "boss@x.com, lead@x.com"
    store.save_rule(saved)
    box["events"] = [ev(5)]
    sender = Sender()
    go(env, T0, sender)
    assert sender.calls[0]["to"] == ["boss@x.com", "lead@x.com"]


def test_daily_cap_records_extra_alerts_as_capped(env):
    settings, store, box, rules, ctx = env
    store.set_setting("email_enabled", "1")
    store.set_setting("daily_cap", "1")
    sender = Sender()
    box["events"] = [ev(5)]
    go(env, T0, sender)
    box["events"] = [ev(5), ev(1, "b")]
    result = go(env, T0 + timedelta(minutes=20), sender)  # past the 15 min cooldown
    assert [r["status"] for r in result.fired] == ["capped"] and len(sender.calls) == 1


def test_failed_email_is_retried_and_logged_once(env):
    settings, store, box, rules, ctx = env
    store.set_setting("email_enabled", "1")
    box["events"] = [ev(5)]
    broken = Sender(fail=True)
    assert go(env, T0, broken).fired[0]["status"] == "failed"
    assert go(env, T0 + timedelta(minutes=5), broken).fired[0]["status"] == "failed"
    assert len(store.alerts(statuses=["failed"])) == 1  # same failure is not logged again within the hour
    fixed = Sender()
    assert go(env, T0 + timedelta(minutes=10), fixed).fired[0]["status"] == "sent"  # kept its events, retried


def test_dry_run_records_and_sends_nothing(env):
    settings, store, box, rules, ctx = env
    store.set_setting("email_enabled", "1")
    box["events"] = [ev(5)]
    sender = Sender()
    result = go(env, T0, sender, dry_run=True)
    assert result.fired[0]["status"] == "would_send" and sender.calls == [] and store.alerts() == []


def test_disabled_rule_is_not_checked(env):
    settings, store, box, rules, ctx = env
    store.seed_rules({r.id: r.default_config() for r in rules.values()})
    disabled = store.get_rules()["outside_attempt"]
    disabled.enabled = False
    store.save_rule(disabled)
    box["fail"] = True  # would error if it were collected
    assert go(env, T0, Sender()).rule_errors == {}


def test_system_error_email_after_consecutive_failures_then_cooldown(env):
    settings, store, box, rules, ctx = env
    system = make_rule("system_error", name="System", source="system", collect=None, group="system", threshold=3)
    rules = {**env[3], "system_error": system}
    store.set_setting("email_enabled", "1")
    box["fail"] = True
    sender = Sender()

    def step(i):
        return run_once(settings, store, now=T0 + timedelta(minutes=5 * i), rules=rules, ctx=ctx, sender=sender)

    assert step(0).ok is False and sender.calls == []
    step(1)
    assert sender.calls == []  # two failures are not enough
    step(2)
    assert len(sender.calls) == 1 and "neon" in sender.calls[0]["subject"]
    step(3)
    assert len(sender.calls) == 1  # silent during the 6 h cooldown


# ------------------------------------------------------------- interval, suggestions, state path
def test_window_is_never_shorter_than_the_check_interval():
    assert effective_config(cfg(1, window=5), 15).window_minutes == 15
    assert effective_config(cfg(1, window=60), 15).window_minutes == 60
    assert effective_config(cfg(1, window=0), 15).window_minutes == 0  # system rule has no window


def test_a_short_window_does_not_lose_events_between_slow_checks(env):
    settings, store, box, rules, ctx = env
    store.set_setting("email_enabled", "1")
    store.set_setting("check_interval_minutes", "15")
    seed_defaults(store, rules)
    store.save_group(GroupConfig("activity", 5, 15))  # a 5 min window on a 15 min check interval
    box["events"] = [ev(12)]  # happened 12 min ago: outside a 5 min window, inside the 15 min check gap
    assert go(env, T0, Sender()).fired[0]["status"] == "sent"


def test_runtime_check_interval_defaults_to_15_and_follows_the_dashboard_setting():
    store = Store(":memory:")
    assert Runtime.load(Settings(), store).check_interval_minutes == 15
    store.set_setting("check_interval_minutes", "5")
    assert Runtime.load(Settings(), store).check_interval_minutes == 5


def test_state_path_is_anchored_to_the_project_not_the_launch_folder(monkeypatch):
    from pathlib import Path

    from habit10x_marketing_automation.config import PROJECT_ROOT

    monkeypatch.delenv("TRACTION_DB_PATH", raising=False)
    assert Settings(traction_db_path="data/traction.db").state_path == PROJECT_ROOT / "data" / "traction.db"
    absolute = Path.cwd() / "x.db"
    assert Settings(traction_db_path=str(absolute)).state_path == absolute


# --------------------------------------------------------------------------------------- groups
def test_alerts_in_a_group_share_window_and_cooldown_but_keep_their_own_threshold():
    store = Store(":memory:")
    rules = {"a": make_rule("a", group="traffic", threshold=3), "b": make_rule("b", group="traffic", threshold=10),
             "c": make_rule("c", group="activity")}
    seed_defaults(store, rules)
    store.save_group(GroupConfig("traffic", 30, 45))
    configs = load_configs(store, rules)
    assert [(configs[r].window_minutes, configs[r].cooldown_minutes) for r in "ab"] == [(30, 45)] * 2
    assert (configs["a"].threshold, configs["b"].threshold) == (3, 10)  # not forced to one value
    assert configs["c"].window_minutes == 60  # the other group is untouched


def test_changing_one_alerts_threshold_leaves_the_others_alone():
    store = Store(":memory:")
    rules = {"a": make_rule("a", group="traffic", threshold=3), "b": make_rule("b", group="traffic", threshold=10)}
    seed_defaults(store, rules)
    stored = store.get_rules()
    stored["a"].threshold = 7
    store.save_rule(stored["a"])
    configs = load_configs(store, rules)
    assert (configs["a"].threshold, configs["b"].threshold) == (7, 10)


def test_seeding_never_overwrites_an_edited_group():
    store = Store(":memory:")
    rules = {"a": make_rule("a", group="traffic")}
    seed_defaults(store, rules)
    store.save_group(GroupConfig("traffic", 90, 30))
    seed_defaults(store, rules)
    assert store.get_groups()["traffic"].window_minutes == 90


def test_upgrade_from_the_group_only_version_keeps_edits_and_restores_defaults(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE groups (group_id TEXT PRIMARY KEY, threshold INTEGER, window_minutes INTEGER, "
               "cooldown_minutes INTEGER)")
    db.executemany("INSERT INTO groups VALUES (?,?,?,?)",
                   [("activity", 1, 60, 15), ("traffic", 8, 30, 20), ("system", 3, 0, 360)])  # traffic was edited
    db.commit()
    db.close()

    store = Store(path)
    rules = {"a": make_rule("a", group="traffic", threshold=3), "b": make_rule("b", group="activity", threshold=2)}
    seed_defaults(store, rules)
    configs = load_configs(store, rules)
    assert configs["a"].threshold == 8  # the group-wide value the user had chosen is kept
    assert configs["b"].threshold == 2  # untouched group: back to the alert's own default
    assert (configs["a"].window_minutes, configs["a"].cooldown_minutes) == (30, 20)  # window/cooldown carried over

    stored = store.get_rules()
    stored["a"].threshold = 4
    store.save_rule(stored["a"])
    seed_defaults(store, rules)  # runs on every check: must not repeat the migration
    assert load_configs(store, rules)["a"].threshold == 4


def test_rolling_counts_gives_the_number_the_threshold_is_compared_with():
    rule = make_rule(count_mode="actors", per_entity=True)
    step = timedelta(minutes=15)
    events = [Event(T0 - timedelta(minutes=m), e, a, {}) for m, e, a in
              [(50, "x", "v1"), (49, "x", "v2"), (48, "x", "v3"), (20, "y", "v9")]]
    series = dict(rolling_counts(rule, 60, events, T0 - timedelta(hours=2), T0, step))
    assert series[T0] == 3  # busiest item inside the last 60 minutes is "x" with 3 visitors
    assert series[T0 - timedelta(hours=2) + step] == 0  # before anything happened
    assert len(series) == 8
