"""Alert rule definitions: what each rule counts, where the data comes from, and its default settings.

A rule's collector returns raw events inside a time range. The engine (engine.py) decides whether
the count reaches the threshold; collectors only fetch and shape data.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings
from .neon import Neon
from .openpanel import OpenPanel
from .store import RuleConfig

UTC = timezone.utc


@dataclass(frozen=True, order=True)
class Event:
    ts: datetime
    entity: str = field(compare=False, default="")  # grouping key for per-item rules ("" = global)
    actor: str = field(compare=False, default="")  # unique person/visitor/row id (for distinct counts)
    info: dict = field(compare=False, default_factory=dict)  # details shown in the email


class Context:
    """Lazily opened data sources plus the exclusion lists. A failed connection is remembered for the run."""

    def __init__(self, settings: Settings, test_domains: list[str], team_emails: list[str],
                 neon_factory: Callable[[], Neon] | None = None,
                 openpanel_factory: Callable[[], OpenPanel] | None = None):
        self.settings = settings
        self.test_domains = [d.strip().lower() for d in test_domains if d.strip()]
        self.team_emails = [e.strip().lower() for e in team_emails if e.strip()]
        self._factories = {"neon": neon_factory or (lambda: Neon(settings)),
                           "openpanel": openpanel_factory or (lambda: OpenPanel(settings))}
        self._open: dict[str, Any] = {}
        self._failed: dict[str, Exception] = {}

    def _get(self, name: str):
        if name in self._failed:
            raise self._failed[name]
        if name not in self._open:
            try:
                self._open[name] = self._factories[name]()
            except Exception as exc:  # noqa: BLE001 - remembered so one outage is not retried per rule
                self._failed[name] = exc
                raise
        return self._open[name]

    @property
    def neon(self) -> Neon:
        return self._get("neon")

    @property
    def openpanel(self) -> OpenPanel:
        return self._get("openpanel")

    @property
    def exclusions(self) -> dict[str, list[str]]:
        return {"test_domains": self.test_domains, "team": self.team_emails}

    def close(self) -> None:
        for source in self._open.values():
            try:
                source.close()
            except Exception:  # noqa: BLE001
                pass
        self._open.clear()


def _excl(col: str) -> str:
    """SQL fragment that drops test-domain and team addresses (guests with no email pass)."""
    return (f"split_part(lower(coalesce({col}, '')), '@', 2) <> ALL(%(test_domains)s::text[]) "
            f"AND lower(coalesce({col}, '')) <> ALL(%(team)s::text[])")


def _q(ctx: Context, sql: str, since: datetime, until: datetime, **extra) -> list[dict]:
    return ctx.neon.query(sql, {"since": since, "until": until, **ctx.exclusions, **extra})


def _utc(ts: datetime) -> datetime:
    return ts.astimezone(UTC)


def _title(row: dict) -> str:
    return row.get("title") or ""


# ---------------------------------------------------------------------------------- Neon rules
def collect_outside_attempt(ctx: Context, since, until, params) -> list[Event]:
    rows = _q(ctx, f"""
        SELECT r.id::text AS id, r.created_at AS ts, sc.challenge_number AS num, sc.title,
               r.response_type, r.wit_score, r.user_name, r.user_email, (r.guest_id IS NOT NULL) AS guest
        FROM social_challenge_responses r JOIN social_challenges sc ON sc.id = r.challenge_id
        WHERE r.created_at >= %(since)s AND r.created_at < %(until)s
          AND (r.source = 'public' OR r.guest_id IS NOT NULL) AND {_excl('r.user_email')}
        ORDER BY r.created_at""", since, until)
    return [Event(_utc(r["ts"]), "", r["id"], r) for r in rows]


def collect_new_signup(ctx: Context, since, until, params) -> list[Event]:
    rows = _q(ctx, f"""
        SELECT u.email, u.name, u.created_at AS ts, u.country, u.plan,
               a.first_touch_platform AS platform, a.first_touch_source_slug AS slug
        FROM challenge_users u LEFT JOIN user_attribution_summary a ON lower(a.user_id) = lower(u.email)
        WHERE u.created_at >= %(since)s AND u.created_at < %(until)s AND {_excl('u.email')}
        ORDER BY u.created_at""", since, until)
    return [Event(_utc(r["ts"]), "", r["email"], r) for r in rows]


def collect_plan_started(ctx: Context, since, until, params) -> list[Event]:
    rows = _q(ctx, f"""
        SELECT u.email, u.name, u.plan, u.subscription_status AS status, u.plan_started_at AS ts
        FROM challenge_users u
        WHERE u.plan IS NOT NULL AND u.plan_started_at >= %(since)s AND u.plan_started_at < %(until)s
          AND {_excl('u.email')}
        ORDER BY u.plan_started_at""", since, until)
    return [Event(_utc(r["ts"]), "", r["email"], r) for r in rows]


def collect_great_response(ctx: Context, since, until, params) -> list[Event]:
    rows = _q(ctx, f"""
        SELECT r.id::text AS id, r.created_at AS ts, sc.challenge_number AS num, sc.title,
               r.response_type, r.wit_score, r.user_name, r.user_email
        FROM social_challenge_responses r JOIN social_challenges sc ON sc.id = r.challenge_id
        WHERE r.created_at >= %(since)s AND r.created_at < %(until)s
          AND r.wit_score >= %(min_score)s AND {_excl('r.user_email')}
        ORDER BY r.created_at""", since, until, min_score=float(params.get("min_score", 90)))
    return [Event(_utc(r["ts"]), "", r["id"], r) for r in rows]


def collect_source_visitors(ctx: Context, since, until, params) -> list[Event]:
    rows = _q(ctx, """
        SELECT e.created_at AS ts, e.source_slug AS slug, coalesce(e.platform, s.platform) AS platform,
               s.account_name, e.attribution_id AS visitor
        FROM attribution_event e LEFT JOIN source_links s ON s.source_slug = e.source_slug
        WHERE e.created_at >= %(since)s AND e.created_at < %(until)s AND e.source_slug IS NOT NULL
        ORDER BY e.created_at""", since, until)
    return [Event(_utc(r["ts"]), r["slug"], r["visitor"], r) for r in rows]


def collect_source_signup(ctx: Context, since, until, params) -> list[Event]:
    rows = _q(ctx, f"""
        SELECT u.email, u.name, u.created_at AS ts, a.first_touch_platform AS platform,
               a.first_touch_source_slug AS slug
        FROM challenge_users u JOIN user_attribution_summary a ON lower(a.user_id) = lower(u.email)
        WHERE u.created_at >= %(since)s AND u.created_at < %(until)s
          AND a.first_touch_source_slug IS NOT NULL AND {_excl('u.email')}
        ORDER BY u.created_at""", since, until)
    return [Event(_utc(r["ts"]), "", r["email"], r) for r in rows]


def collect_challenge_visitors(ctx: Context, since, until, params) -> list[Event]:
    rows = _q(ctx, """
        SELECT e.created_at AS ts, e.challenge_id AS entity, e.attribution_id AS visitor,
               sc.challenge_number AS num, sc.title
        FROM attribution_event e LEFT JOIN social_challenges sc ON sc.id::text = e.challenge_id
        WHERE e.created_at >= %(since)s AND e.created_at < %(until)s AND e.challenge_id IS NOT NULL
        ORDER BY e.created_at""", since, until)
    return [Event(_utc(r["ts"]), r["entity"], r["visitor"], r) for r in rows]


def collect_attempts_surge(ctx: Context, since, until, params) -> list[Event]:
    rows = _q(ctx, f"""
        SELECT r.created_at AS ts, r.challenge_id::text AS entity,
               coalesce(nullif(lower(r.user_email), ''), r.guest_id, r.id::text) AS person,
               sc.challenge_number AS num, sc.title
        FROM social_challenge_responses r JOIN social_challenges sc ON sc.id = r.challenge_id
        WHERE r.created_at >= %(since)s AND r.created_at < %(until)s AND {_excl('r.user_email')}
        ORDER BY r.created_at""", since, until)
    return [Event(_utc(r["ts"]), r["entity"], r["person"], r) for r in rows]


# ------------------------------------------------------------------------------ OpenPanel rules
def _op_events(ctx: Context, name: str, since: datetime, until: datetime, max_pages: int = 30) -> list[dict]:
    """All events called `name` created in [since, until). Dates are day-granular, so filter by time here."""
    start = _utc(since).date().isoformat()
    end = (_utc(until).date() + timedelta(days=1)).isoformat()  # use tomorrow so today's events are included
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        response = ctx.openpanel.export_events(event=name, start=start, end=end, page=page, limit=200,
                                               includes=["properties"])
        oldest = None
        for e in response.get("data", []):
            ts = datetime.fromisoformat(e["createdAt"]).astimezone(UTC)
            oldest = ts if oldest is None else min(oldest, ts)
            if _utc(since) <= ts < _utc(until):
                out.append({**e, "_ts": ts})
        if page >= response.get("meta", {}).get("pages", 0):
            break
        if oldest is not None and oldest < _utc(since):  # results are newest-first: nothing older can match
            break
    return out


def collect_pricing_interest(ctx: Context, since, until, params) -> list[Event]:
    events = []
    for e in _op_events(ctx, "pricing_upgrade_clicked", since, until):
        props = e.get("properties") or {}
        if str(props.get("alreadyPremium", "")).lower() in ("true", "1"):
            continue
        events.append(Event(e["_ts"], "", e["id"], {"path": e.get("path"), "country": e.get("country"),
                                                    "device": f"{e.get('os') or ''} {e.get('browser') or ''}".strip()}))
    return sorted(events)


def collect_site_visitors(ctx: Context, since, until, params) -> list[Event]:
    return sorted(Event(e["_ts"], "", e.get("deviceId") or e.get("sessionId") or e["id"],
                        {"path": e.get("path"), "country": e.get("country")})
                  for e in _op_events(ctx, "screen_view", since, until))


# ------------------------------------------------------------------------------------ definitions
@dataclass(frozen=True)
class GroupDef:
    """Similar alerts share one window and one cooldown. Each alert keeps its own threshold."""
    id: str
    name: str
    description: str
    window_minutes: int
    cooldown_minutes: int


GROUP_LIST: list[GroupDef] = [
    GroupDef("activity", "Activity alerts", "Someone did something. Every occurrence counts.", 60, 15),
    GroupDef("traffic", "Traffic and attention alerts", "Counts different visitors or people inside the window.",
             60, 60),
    GroupDef("system", "System health", "The checker could not read our data.", 0, 360),
]
GROUPS: dict[str, GroupDef] = {g.id: g for g in GROUP_LIST}


@dataclass(frozen=True)
class RuleDef:
    id: str
    name: str
    description: str  # one line: what this alert means
    source: str  # "neon" | "openpanel" | "system"
    count_mode: str  # "events" counts every event; "actors" counts distinct people/visitors
    per_entity: bool
    unit: tuple[str, str]  # (singular, plural) used in emails
    collect: Callable[[Context, datetime, datetime, dict], list[Event]] | None
    group: str = "activity"
    threshold: int = 1  # this alert's own starting threshold (editable per alert in the dashboard)
    params: dict = field(default_factory=dict)
    settle_seconds: int = 0  # ignore the newest N seconds (lets related rows, e.g. attribution, land first)

    def default_config(self) -> RuleConfig:
        g = GROUPS[self.group]
        return RuleConfig(self.id, True, self.threshold, g.window_minutes, g.cooldown_minutes, "", dict(self.params))


RULE_LIST: list[RuleDef] = [
    RuleDef("outside_attempt", "Outside visitor tried a challenge",
            "Someone outside the team answered a challenge from the public page or as a guest.",
            "neon", "events", False, ("attempt", "attempts"), collect_outside_attempt, threshold=1),
    RuleDef("new_signup", "New signup", "A new real user signed up.",
            "neon", "events", False, ("signup", "signups"), collect_new_signup, threshold=1, settle_seconds=120),
    RuleDef("plan_started", "Plan started", "A user started a plan.",
            "neon", "events", False, ("plan start", "plan starts"), collect_plan_started, threshold=1),
    RuleDef("great_response", "Great response", "A response scored 90 or more out of 100 - worth featuring.",
            "neon", "events", False, ("response", "responses"), collect_great_response, threshold=1,
            params={"min_score": 90}),
    RuleDef("source_signup", "Signup from a social link", "A new user whose first visit came through one of our links.",
            "neon", "events", False, ("signup", "signups"), collect_source_signup, threshold=1, settle_seconds=120),
    RuleDef("pricing_interest", "Interested in paying", "Someone not on a paid plan clicked Upgrade.",
            "openpanel", "events", False, ("click", "clicks"), collect_pricing_interest, threshold=1),
    RuleDef("source_visitors", "Visitors from a social link", "Different visitors arriving through one of our links "
            "(counted per link).", "neon", "actors", True, ("visitor", "visitors"), collect_source_visitors,
            group="traffic", threshold=1),
    RuleDef("challenge_visitors", "Challenge page spike", "Different visitors on one challenge (per challenge).",
            "neon", "actors", True, ("visitor", "visitors"), collect_challenge_visitors, group="traffic", threshold=5),
    RuleDef("attempts_surge", "Many people on one challenge", "Different people answering the same challenge.",
            "neon", "actors", True, ("person", "people"), collect_attempts_surge, group="traffic", threshold=3),
    RuleDef("site_visitors", "Site-wide surge", "Different visitors anywhere on the site.",
            "openpanel", "actors", False, ("visitor", "visitors"), collect_site_visitors, group="traffic",
            threshold=10),
    RuleDef("system_error", "System cannot read data",
            "One email when Neon or OpenPanel cannot be read this many checks in a row.", "system", "events", False,
            ("failed check", "failed checks"), None, group="system", threshold=3),
]

RULES: dict[str, RuleDef] = {r.id: r for r in RULE_LIST}

# The social-media alert has no threshold box on the alert screen (its three numbers live on the
# Social tracker page), so it is kept out of RULE_LIST; it still needs a definition for emails and History.
SOCIAL_RULE = RuleDef("social_traction", "Video reached a threshold",
                      "A tracked video passed the views, likes or comments threshold.",
                      "social", "events", False, ("milestone", "milestones"), None)

RULE_NAMES: dict[str, str] = {**{r.id: r.name for r in RULE_LIST}, SOCIAL_RULE.id: SOCIAL_RULE.name}
