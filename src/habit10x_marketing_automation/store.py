"""Local state: rule settings, alert history, "already alerted" markers and run log (SQLite).

A separate file (TRACTION_DB_PATH) keeps the production database strictly read-only. SQLite is
opened in WAL mode so the checker and the dashboard can use the same file at the same time.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    rule_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, threshold INTEGER NOT NULL,
    window_minutes INTEGER NOT NULL, cooldown_minutes INTEGER NOT NULL,
    recipients TEXT NOT NULL DEFAULT '', params TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS group_settings (
    group_id TEXT PRIMARY KEY, window_minutes INTEGER NOT NULL, cooldown_minutes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS watermarks (
    rule_id TEXT NOT NULL, entity TEXT NOT NULL, ts TEXT NOT NULL, PRIMARY KEY (rule_id, entity)
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, fired_at TEXT NOT NULL, rule_id TEXT NOT NULL, entity TEXT NOT NULL,
    count INTEGER NOT NULL, threshold INTEGER NOT NULL, window_minutes INTEGER NOT NULL,
    status TEXT NOT NULL, subject TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_alerts_rule ON alerts (rule_id, entity, fired_at);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT,
    ok INTEGER NOT NULL DEFAULT 0, sources TEXT NOT NULL DEFAULT '{}', fired INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS social_videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, external_id TEXT NOT NULL, url TEXT NOT NULL,
    original_url TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '',
    posted_at TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '{}', active INTEGER NOT NULL DEFAULT 1,
    added_at TEXT NOT NULL, UNIQUE (platform, external_id)
);
CREATE TABLE IF NOT EXISTS social_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT, video_id INTEGER NOT NULL, fetched_at TEXT NOT NULL,
    views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER, ok INTEGER NOT NULL,
    error TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_social_stats_video ON social_stats (video_id, id);
CREATE TABLE IF NOT EXISTS social_threshold_alerts (
    video_id INTEGER NOT NULL, metric TEXT NOT NULL, threshold INTEGER NOT NULL, fired_at TEXT NOT NULL,
    PRIMARY KEY (video_id, metric, threshold)
);
CREATE TABLE IF NOT EXISTS rule_status (
    rule_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL, current_count INTEGER NOT NULL DEFAULT 0,
    top_entity TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT ''
);
"""

# Alert statuses: sent | recorded (email switch off) | capped (daily limit) | failed (will retry)
STATUSES = ("sent", "recorded", "capped", "failed")


@dataclass
class RuleConfig:
    rule_id: str
    enabled: bool
    threshold: int
    window_minutes: int
    cooldown_minutes: int
    recipients: str = ""
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class GroupConfig:
    """Window and cooldown shared by all alerts of a group."""
    group_id: str
    window_minutes: int
    cooldown_minutes: int


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._migrate_legacy_groups()

    def close(self) -> None:
        self._db.close()

    # ---- rules -------------------------------------------------------------------------
    def seed_rules(self, defaults: dict[str, RuleConfig]) -> None:
        """Insert default settings for rules that do not have a row yet (never overwrites edits)."""
        for cfg in defaults.values():
            self._db.execute(
                "INSERT OR IGNORE INTO rules VALUES (?,?,?,?,?,?,?)",
                (cfg.rule_id, int(cfg.enabled), cfg.threshold, cfg.window_minutes, cfg.cooldown_minutes,
                 cfg.recipients, json.dumps(cfg.params)),
            )
        self._db.commit()

    def get_rules(self) -> dict[str, RuleConfig]:
        rows = self._db.execute("SELECT * FROM rules").fetchall()
        return {r["rule_id"]: RuleConfig(r["rule_id"], bool(r["enabled"]), r["threshold"], r["window_minutes"],
                                         r["cooldown_minutes"], r["recipients"], json.loads(r["params"]))
                for r in rows}

    def save_rule(self, cfg: RuleConfig) -> None:
        self._db.execute(
            "UPDATE rules SET enabled=?, threshold=?, window_minutes=?, cooldown_minutes=?, recipients=?, params=? "
            "WHERE rule_id=?",
            (int(cfg.enabled), cfg.threshold, cfg.window_minutes, cfg.cooldown_minutes, cfg.recipients,
             json.dumps(cfg.params), cfg.rule_id),
        )
        self._db.commit()

    # ---- groups (one threshold / window / cooldown for all similar alerts) --------------
    def seed_groups(self, defaults: dict[str, GroupConfig]) -> None:
        for g in defaults.values():
            self._db.execute("INSERT OR IGNORE INTO group_settings VALUES (?,?,?)",
                             (g.group_id, g.window_minutes, g.cooldown_minutes))
        self._db.commit()

    def get_groups(self) -> dict[str, GroupConfig]:
        rows = self._db.execute("SELECT * FROM group_settings").fetchall()
        return {r["group_id"]: GroupConfig(r["group_id"], r["window_minutes"], r["cooldown_minutes"]) for r in rows}

    def save_group(self, g: GroupConfig) -> None:
        self._db.execute("UPDATE group_settings SET window_minutes=?, cooldown_minutes=? WHERE group_id=?",
                         (g.window_minutes, g.cooldown_minutes, g.group_id))
        self._db.commit()

    def _legacy_table(self) -> bool:
        return self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='groups'").fetchone() is not None

    def _migrate_legacy_groups(self) -> None:
        """An earlier version kept threshold + window + cooldown per group in `groups`. Carry window and cooldown over."""
        if self._legacy_table():
            self._db.execute("INSERT OR IGNORE INTO group_settings SELECT group_id, window_minutes, cooldown_minutes "
                             "FROM groups")
            self._db.commit()

    def legacy_group_thresholds(self) -> dict[str, int]:
        """Group-wide thresholds saved by that earlier version (empty if there never was one)."""
        if not self._legacy_table():
            return {}
        return {r["group_id"]: r["threshold"] for r in self._db.execute("SELECT group_id, threshold FROM groups")}

    # ---- settings ----------------------------------------------------------------------
    def get_setting(self, key: str, default: str = "") -> str:
        row = self._db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, value))
        self._db.commit()

    # ---- watermarks (reset-after-alert) ------------------------------------------------
    def get_watermarks(self, rule_id: str) -> dict[str, datetime]:
        rows = self._db.execute("SELECT entity, ts FROM watermarks WHERE rule_id=?", (rule_id,)).fetchall()
        return {r["entity"]: _parse(r["ts"]) for r in rows}

    def set_watermark(self, rule_id: str, entity: str, ts: datetime) -> None:
        self._db.execute(
            "INSERT INTO watermarks VALUES (?,?,?) ON CONFLICT(rule_id, entity) DO UPDATE SET ts=excluded.ts",
            (rule_id, entity, _iso(ts)))
        self._db.commit()

    # ---- alerts ------------------------------------------------------------------------
    def add_alert(self, *, fired_at: datetime, rule_id: str, entity: str, count: int, threshold: int,
                  window_minutes: int, status: str, subject: str, detail: list[dict] | None = None,
                  error: str = "") -> int:
        cur = self._db.execute(
            "INSERT INTO alerts (fired_at, rule_id, entity, count, threshold, window_minutes, status, subject, detail, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_iso(fired_at), rule_id, entity, count, threshold, window_minutes, status, subject,
             json.dumps(detail or [], default=str), error))
        self._db.commit()
        return int(cur.lastrowid)

    def last_fired(self, rule_id: str) -> dict[str, datetime]:
        """Latest non-failed alert time per entity (cooldown clock)."""
        rows = self._db.execute(
            "SELECT entity, MAX(fired_at) AS t FROM alerts WHERE rule_id=? AND status != 'failed' GROUP BY entity",
            (rule_id,)).fetchall()
        return {r["entity"]: _parse(r["t"]) for r in rows}

    def last_alert(self, rule_id: str, entity: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM alerts WHERE rule_id=? AND entity=? ORDER BY id DESC LIMIT 1",
                                (rule_id, entity)).fetchone()

    def count_sent_since(self, since: datetime) -> int:
        return self._db.execute("SELECT COUNT(*) FROM alerts WHERE status='sent' AND fired_at >= ?",
                                (_iso(since),)).fetchone()[0]

    def alerts(self, limit: int = 200, rule_id: str | None = None, statuses: list[str] | None = None) -> list[dict]:
        sql, args = "SELECT * FROM alerts WHERE 1=1", []
        if rule_id:
            sql += " AND rule_id=?"
            args.append(rule_id)
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += statuses
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    # ---- social media tracker ----------------------------------------------------------
    def add_social_videos(self, items: list[dict], now: datetime, reactivate: bool = True) -> tuple[int, int]:
        """items: {platform, external_id, url, original, meta}. Returns (added, already_tracked).
        A removed video comes back when imported again, unless reactivate=False (the automatic import)."""
        added = existing = 0
        for it in items:
            row = self._db.execute("SELECT id, active FROM social_videos WHERE platform=? AND external_id=?",
                                   (it["platform"], it["external_id"])).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO social_videos (platform, external_id, url, original_url, meta, added_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (it["platform"], it["external_id"], it["url"], it.get("original", ""),
                     json.dumps(it.get("meta") or {}, default=str), _iso(now)))
                added += 1
            elif not row["active"] and reactivate:
                self._db.execute("UPDATE social_videos SET active=1 WHERE id=?", (row["id"],))
                added += 1
            else:
                existing += 1
        self._db.commit()
        return added, existing

    def social_videos(self, active_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM social_videos" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
        return [{**dict(r), "meta": json.loads(r["meta"])} for r in self._db.execute(sql).fetchall()]

    def remove_social_video(self, video_id: int) -> None:
        self._db.execute("UPDATE social_videos SET active=0 WHERE id=?", (video_id,))
        self._db.commit()

    def update_social_video_info(self, video_id: int, title: str = "", author: str = "", posted_at: str = "") -> None:
        """Fill in what the platform told us; never blank out something we already know."""
        self._db.execute(
            "UPDATE social_videos SET title=COALESCE(NULLIF(?, ''), title), author=COALESCE(NULLIF(?, ''), author), "
            "posted_at=COALESCE(NULLIF(?, ''), posted_at) WHERE id=?", (title, author, posted_at, video_id))
        self._db.commit()

    def add_social_stat(self, video_id: int, fetched_at: datetime, *, views: int | None = None,
                        likes: int | None = None, comments: int | None = None, shares: int | None = None,
                        ok: bool = True, error: str = "", source: str = "") -> None:
        self._db.execute(
            "INSERT INTO social_stats (video_id, fetched_at, views, likes, comments, shares, ok, error, source) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (video_id, _iso(fetched_at), views, likes, comments, shares, int(ok), error, source))
        self._db.commit()

    def social_overview(self) -> list[dict]:
        """Every tracked video with its newest numbers: `latest` / `previous` (successful checks) and `attempt`
        (the most recent check, successful or not)."""
        videos = self.social_videos()
        ok_rows = self._db.execute(
            "SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY video_id ORDER BY id DESC) AS rn "
            "FROM social_stats WHERE ok=1) WHERE rn <= 2").fetchall()
        attempts = self._db.execute(
            "SELECT * FROM social_stats WHERE id IN (SELECT MAX(id) FROM social_stats GROUP BY video_id)").fetchall()
        latest, previous = {}, {}
        for r in ok_rows:
            (latest if r["rn"] == 1 else previous)[r["video_id"]] = dict(r)
        attempt = {r["video_id"]: dict(r) for r in attempts}
        return [{**v, "latest": latest.get(v["id"]), "previous": previous.get(v["id"]), "attempt": attempt.get(v["id"])}
                for v in videos]

    def add_social_alert(self, video_id: int, metric: str, threshold: int, fired_at: datetime) -> None:
        """Remember that this video was announced for this metric at this threshold (so only once)."""
        self._db.execute("INSERT OR IGNORE INTO social_threshold_alerts VALUES (?,?,?,?)",
                         (video_id, metric, threshold, _iso(fired_at)))
        self._db.commit()

    def social_alerted(self, metric: str, threshold: int) -> set[int]:
        rows = self._db.execute("SELECT video_id FROM social_threshold_alerts WHERE metric=? AND threshold=?",
                                (metric, threshold)).fetchall()
        return {r["video_id"] for r in rows}

    def social_history(self, video_id: int) -> list[dict]:
        rows = self._db.execute("SELECT * FROM social_stats WHERE video_id=? AND ok=1 ORDER BY id",
                                (video_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---- runs & live status ------------------------------------------------------------
    def start_run(self, started: datetime) -> int:
        cur = self._db.execute("INSERT INTO runs (started_at) VALUES (?)", (_iso(started),))
        self._db.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, finished: datetime, ok: bool, sources: dict[str, str], fired: int) -> None:
        self._db.execute("UPDATE runs SET finished_at=?, ok=?, sources=?, fired=? WHERE id=?",
                         (_iso(finished), int(ok), json.dumps(sources), fired, run_id))
        self._db.commit()

    def recent_runs(self, n: int = 10) -> list[dict]:
        rows = self._db.execute("SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT ?",
                                (n,)).fetchall()
        return [{**dict(r), "sources": json.loads(r["sources"])} for r in rows]

    def set_rule_status(self, rule_id: str, checked_at: datetime, current_count: int, top_entity: str = "",
                        error: str = "") -> None:
        self._db.execute(
            "INSERT INTO rule_status VALUES (?,?,?,?,?) ON CONFLICT(rule_id) DO UPDATE SET checked_at=excluded.checked_at, "
            "current_count=excluded.current_count, top_entity=excluded.top_entity, error=excluded.error",
            (rule_id, _iso(checked_at), current_count, top_entity, error))
        self._db.commit()

    def rule_statuses(self) -> dict[str, dict]:
        return {r["rule_id"]: dict(r) for r in self._db.execute("SELECT * FROM rule_status").fetchall()}


def parse_ts(value: str) -> datetime:
    return _parse(value)


def hours_ago(now: datetime, hours: float) -> datetime:
    return now - timedelta(hours=hours)
