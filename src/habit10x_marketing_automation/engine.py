"""Threshold engine: count events in a window, fire when count >= threshold, then reset the count.

Pure functions (evaluate / live_counts / peak / simulate) hold the logic so they can be tested and
reused by the dashboard; run_once wires them to the data sources, the store and the mailer.
"""

import bisect
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from .config import MissingSettingError, Settings
from .emails import entity_label, render_alert
from .mailer import MailError, send_email
from .rules import GROUPS, RULES, Context, Event, RuleDef
from .store import GroupConfig, RuleConfig, Store

UTC = timezone.utc
STEP = timedelta(minutes=5)
SOURCES = ("neon", "openpanel")


@dataclass
class Firing:
    rule_id: str
    entity: str
    count: int
    events: list[Event]
    newest: datetime


def _count(rule: RuleDef, events: list[Event]) -> int:
    return len({e.actor for e in events}) if rule.count_mode == "actors" else len(events)


def _key(rule: RuleDef, event: Event) -> str:
    return event.entity if rule.per_entity else ""


# ------------------------------------------------------------------------------- pure logic
def evaluate(rule: RuleDef, cfg: RuleConfig, events: list[Event], now: datetime,
             watermarks: dict[str, datetime] | None = None,
             last_fired: dict[str, datetime] | None = None) -> list[Firing]:
    """Which entities have reached the threshold right now?

    Only events inside the window and *newer than the last alert* count (reset-after-alert). An entity
    still inside its cooldown is held back, not dropped: its events fire together once the cooldown ends.
    """
    watermarks, last_fired = watermarks or {}, last_fired or {}
    window_start = now - timedelta(minutes=cfg.window_minutes)
    groups: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        if e.ts < window_start or e.ts > now:
            continue
        key = _key(rule, e)
        mark = watermarks.get(key)
        if mark is not None and e.ts <= mark:
            continue
        groups[key].append(e)
    firings = []
    for key, evs in groups.items():
        count = _count(rule, evs)
        if count < cfg.threshold:
            continue
        last = last_fired.get(key)
        if last is not None and now < last + timedelta(minutes=cfg.cooldown_minutes):
            continue
        evs = sorted(evs)
        firings.append(Firing(rule.id, key, count, evs, evs[-1].ts))
    return firings


def live_counts(rule: RuleDef, cfg: RuleConfig, events: list[Event], now: datetime) -> dict[str, int]:
    """Count per entity inside the window, ignoring alert state ("how close are we right now?")."""
    window_start = now - timedelta(minutes=cfg.window_minutes)
    groups: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        if window_start <= e.ts <= now:
            groups[_key(rule, e)].append(e)
    return {k: _count(rule, v) for k, v in groups.items()}


def peak(rule: RuleDef, window_minutes: int, events: list[Event]) -> tuple[int, str]:
    """Highest count ever seen inside any window of this length, and the entity it happened on."""
    by_entity: dict[str, list[Event]] = defaultdict(list)
    for e in sorted(events):
        by_entity[_key(rule, e)].append(e)
    best, best_key = 0, ""
    span = timedelta(minutes=window_minutes)
    for key, evs in by_entity.items():
        window: deque[Event] = deque()
        actors: Counter = Counter()
        for e in evs:
            window.append(e)
            actors[e.actor] += 1
            while window[0].ts <= e.ts - span:
                old = window.popleft()
                actors[old.actor] -= 1
                if actors[old.actor] == 0:
                    del actors[old.actor]
            n = len(actors) if rule.count_mode == "actors" else len(window)
            if n > best:
                best, best_key = n, key
    return best, best_key


def simulate(rule: RuleDef, cfg: RuleConfig, events: list[Event], start: datetime, end: datetime,
             step: timedelta = STEP) -> list[Firing]:
    """Replay the checker (one check every `step`) over history with the given settings: what would have fired?"""
    events = sorted(events)
    stamps = [e.ts for e in events]
    window = timedelta(minutes=cfg.window_minutes)
    marks: dict[str, datetime] = {}
    last: dict[str, datetime] = {}
    out: list[Firing] = []
    t = start + step
    while t <= end:
        lo, hi = bisect.bisect_left(stamps, t - window), bisect.bisect_right(stamps, t)
        if lo == hi:  # nothing inside the window at this check: nothing can fire
            t += step
            continue
        for f in evaluate(rule, cfg, events[lo:hi], t, marks, last):
            marks[f.entity] = f.newest
            last[f.entity] = t
            out.append(Firing(f.rule_id, f.entity, f.count, f.events, t))  # `newest` = when it would have fired
        t += step
    return out


def effective_config(cfg: RuleConfig, interval_minutes: int) -> RuleConfig:
    """A window shorter than the check interval would let events fall between two checks, so it is raised to it."""
    if cfg.window_minutes and cfg.window_minutes < interval_minutes:
        return replace(cfg, window_minutes=interval_minutes)
    return cfg


def rolling_counts(rule: RuleDef, window_minutes: int, events: list[Event], start: datetime, end: datetime,
                   step: timedelta = STEP) -> list[tuple[datetime, int]]:
    """The count the checker would have seen at every check between start and end (the number the threshold is
    compared with). For per-item alerts it is the busiest item at that moment. Used to draw the graphs."""
    events = sorted(events)
    stamps = [e.ts for e in events]
    window = timedelta(minutes=window_minutes)
    out: list[tuple[datetime, int]] = []
    t = start + step
    while t <= end:
        lo, hi = bisect.bisect_left(stamps, t - window), bisect.bisect_right(stamps, t)
        groups: dict[str, list[Event]] = defaultdict(list)
        for e in events[lo:hi]:
            groups[_key(rule, e)].append(e)
        out.append((t, max((_count(rule, v) for v in groups.values()), default=0)))
        t += step
    return out


# -------------------------------------------------------------------------------- run state
def seed_defaults(store: Store, rules: dict[str, RuleDef]) -> None:
    """Create settings rows for anything that has none yet (never overwrites what was edited)."""
    store.seed_rules({r.id: r.default_config() for r in rules.values()})
    store.seed_groups({g.id: GroupConfig(g.id, g.window_minutes, g.cooldown_minutes) for g in GROUPS.values()})
    _migrate_to_per_alert_thresholds(store, rules)


# What the group-wide thresholds of the previous version defaulted to: a different value means the user edited it.
_OLD_GROUP_THRESHOLD = {"activity": 1, "traffic": 5, "system": 3}


def _migrate_to_per_alert_thresholds(store: Store, rules: dict[str, RuleDef]) -> None:
    """One time: thresholds are per alert again. Start every alert from its own default, but keep a group-wide
    threshold the user had deliberately changed in the group-only version."""
    if store.get_setting("per_alert_thresholds") == "1":
        return
    legacy = store.legacy_group_thresholds()
    stored = store.get_rules()
    for rule in rules.values():
        cfg = stored.get(rule.id)
        if cfg is None:
            continue
        edited = legacy.get(rule.group)
        cfg.threshold = edited if edited is not None and edited != _OLD_GROUP_THRESHOLD.get(rule.group) else rule.threshold
        store.save_rule(cfg)
    store.set_setting("per_alert_thresholds", "1")


def load_configs(store: Store, rules: dict[str, RuleDef]) -> dict[str, RuleConfig]:
    """Per-alert settings (own threshold and on/off) with the window and cooldown of the alert's group applied."""
    groups, out = store.get_groups(), {}
    for rule_id, cfg in store.get_rules().items():
        rule = rules.get(rule_id)
        g = groups.get(rule.group) if rule else None
        out[rule_id] = replace(cfg, window_minutes=g.window_minutes, cooldown_minutes=g.cooldown_minutes) if g else cfg
    return out


@dataclass
class Runtime:
    """Settings that can be changed in the dashboard (stored in SQLite) with .env as the fallback."""
    email_enabled: bool
    recipients: list[str]
    test_domains: list[str]
    team_emails: list[str]
    daily_cap: int
    check_interval_minutes: int = 15

    @classmethod
    def load(cls, settings: Settings, store: Store) -> "Runtime":
        def csv(key: str, fallback: str | None) -> list[str]:
            raw = store.get_setting(key, fallback or "")
            return [x.strip() for x in raw.replace("\n", ",").split(",") if x.strip()]

        return cls(
            email_enabled=store.get_setting("email_enabled", "0") == "1",
            recipients=csv("recipients", settings.alert_recipients),
            test_domains=csv("test_domains", settings.test_email_domains),
            team_emails=csv("team_emails", settings.team_emails),
            daily_cap=int(store.get_setting("daily_cap", "20") or 20),
            check_interval_minutes=max(1, int(store.get_setting("check_interval_minutes", "15") or 15)),
        )


@dataclass
class RunResult:
    started: datetime
    dry_run: bool
    fired: list[dict] = field(default_factory=list)
    source_errors: dict[str, str] = field(default_factory=dict)
    rule_errors: dict[str, str] = field(default_factory=dict)
    live: dict[str, tuple[int, str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.source_errors


def _recipients(cfg: RuleConfig, rt: Runtime) -> list[str]:
    own = [x.strip() for x in cfg.recipients.replace("\n", ",").split(",") if x.strip()]
    return own or rt.recipients


def _dispatch(store: Store, settings: Settings, rt: Runtime, rule: RuleDef, cfg: RuleConfig, firing: Firing,
              now: datetime, sent_so_far: list[int], sender: Callable[..., Any], dry_run: bool) -> dict:
    """Turn one firing into an email / record. Returns a summary row. Never raises."""
    subject, html_body, text_body = render_alert(rule, cfg, firing, settings)
    status, error = "recorded", ""
    if dry_run:
        status = "would_send" if rt.email_enabled else "would_record"
    elif not rt.email_enabled:
        status = "recorded"
    elif sent_so_far[0] >= rt.daily_cap:
        status = "capped"
    else:
        recipients = _recipients(cfg, rt)
        if not recipients:
            status, error = "failed", "No recipients configured (Settings tab)"
        else:
            try:
                sender(settings, subject, html_body, text_body, to=recipients)
                status = "sent"
                sent_so_far[0] += 1
            except (MailError, MissingSettingError, OSError, httpx.HTTPError) as exc:
                status, error = "failed", f"{type(exc).__name__}: {exc}"[:300]

    row = {"rule_id": rule.id, "entity": firing.entity, "count": firing.count, "status": status,
           "subject": subject, "error": error}
    if dry_run:
        return row
    previous = store.last_alert(rule.id, firing.entity)
    repeated_failure = (status == "failed" and previous is not None and previous["status"] == "failed"
                        and previous["error"] == error
                        and datetime.fromisoformat(previous["fired_at"]) > now - timedelta(hours=1))
    if not repeated_failure:  # a retry that fails the same way is not logged again within the hour
        store.add_alert(fired_at=now, rule_id=rule.id, entity=firing.entity, count=firing.count,
                        threshold=cfg.threshold, window_minutes=cfg.window_minutes, status=status, subject=subject,
                        detail=[e.info for e in firing.events[-20:]], error=error)
    if status != "failed":  # failed alerts keep their events so the next run retries them
        store.set_watermark(rule.id, firing.entity, firing.newest)
    return row


dispatch = _dispatch  # public name for the social tracker's likes alert


def run_once(settings: Settings, store: Store, *, now: datetime | None = None, dry_run: bool = False,
             rules: dict[str, RuleDef] | None = None, ctx: Context | None = None,
             sender: Callable[..., Any] = send_email) -> RunResult:
    """One checker pass: collect -> evaluate -> email/record. `dry_run` changes nothing and sends nothing."""
    now = now or datetime.now(UTC)
    rules = rules or RULES
    seed_defaults(store, rules)
    cfgs = load_configs(store, rules)
    rt = Runtime.load(settings, store)
    own_ctx = ctx is None
    ctx = ctx or Context(settings, rt.test_domains, rt.team_emails)
    result = RunResult(started=now, dry_run=dry_run)
    run_id = None if dry_run else store.start_run(now)
    sent = [store.count_sent_since(now - timedelta(hours=24))]
    uses: set[str] = set()

    try:
        for rule in rules.values():
            cfg = cfgs.get(rule.id)
            if cfg is None or rule.collect is None or not cfg.enabled:
                continue
            cfg = effective_config(cfg, rt.check_interval_minutes)
            uses.add(rule.source)
            try:
                events = rule.collect(ctx, now - timedelta(minutes=cfg.window_minutes),
                                      now - timedelta(seconds=rule.settle_seconds), cfg.params)
            except Exception as exc:  # noqa: BLE001 - one failing source must not stop the other rules
                message = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"[:300]
                result.rule_errors[rule.id] = message
                result.source_errors.setdefault(rule.source, message)
                if not dry_run:
                    store.set_rule_status(rule.id, now, 0, "", message)
                continue

            counts = live_counts(rule, cfg, events, now)
            top = max(counts.items(), key=lambda kv: kv[1]) if counts else ("", 0)
            sample = next((e.info for e in events if _key(rule, e) == top[0]), {})
            label = entity_label(sample, top[0])
            result.live[rule.id] = (top[1], label)
            if not dry_run:
                store.set_rule_status(rule.id, now, top[1], label)
            for firing in evaluate(rule, cfg, events, now, store.get_watermarks(rule.id), store.last_fired(rule.id)):
                result.fired.append(_dispatch(store, settings, rt, rule, cfg, firing, now, sent, sender, dry_run))
    finally:
        if own_ctx:
            ctx.close()

    if not dry_run:
        sources = {s: result.source_errors.get(s, "ok") for s in SOURCES if s in uses}
        store.finish_run(run_id, datetime.now(UTC), result.ok, sources, len(result.fired))
        _check_system_error(store, settings, rt, cfgs, rules, now, sent, sender, result)
    return result


def _check_system_error(store: Store, settings: Settings, rt: Runtime, cfgs: dict[str, RuleConfig],
                        rules: dict[str, RuleDef], now: datetime, sent: list[int], sender, result: RunResult) -> None:
    """Email once when a data source has failed N checks in a row (cooldown applies, default 6 h)."""
    rule, cfg = rules.get("system_error"), cfgs.get("system_error")
    if rule is None or cfg is None or not cfg.enabled:
        return
    recent = store.recent_runs(cfg.threshold)
    if len(recent) < cfg.threshold:
        return
    last = store.last_fired("system_error")
    for source in SOURCES:
        states = [r["sources"].get(source) for r in recent]
        if any(s is None or s == "ok" for s in states):
            continue
        if source in last and now < last[source] + timedelta(minutes=cfg.cooldown_minutes):
            continue
        event = Event(now, source, source, {"source": source, "error": states[0]})
        firing = Firing("system_error", source, cfg.threshold, [event], now)
        result.fired.append(_dispatch(store, settings, rt, rule, cfg, firing, now, sent, sender, False))
