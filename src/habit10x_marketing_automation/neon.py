"""Read-only access to the Habit10x Neon (Postgres) database.

Read-only is enforced twice: use a read-only DB role, and every transaction here is
opened with BEGIN READ ONLY (works on Neon's pooled connections, unlike the `options`
startup parameter, which the pooler rejects).
"""

from typing import Any

import psycopg
from psycopg.rows import dict_row

from .config import Settings

STATEMENT_TIMEOUT_MS = 30_000


class Neon:
    def __init__(self, settings: Settings):
        settings.require("database_url")
        self._conn = psycopg.connect(
            settings.database_url.get_secret_value(),  # type: ignore[union-attr]
            row_factory=dict_row,
            connect_timeout=15,
        )
        self._conn.read_only = True

    def query(self, sql: str, params: tuple | dict | None = None) -> list[dict[str, Any]]:
        """Run one read-only query and always end the transaction afterwards."""
        try:
            self._conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
            return self._conn.execute(sql, params).fetchall()
        finally:
            self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Neon":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# Tables the alerts read from (names verified against the app's migrations).
KEY_TABLES = [
    "attribution_event",
    "social_challenges",
    "social_challenge_responses",
    "source_links",
    "user_attribution_summary",
    "challenge_users",
]


def describe_login(db: Neon) -> tuple[str, bool]:
    """(role name, whether this login could write to any table we read from). One quick query."""
    row = db.query(
        "SELECT current_user AS role, coalesce(bool_or(has_table_privilege(current_user, 'public.' || t, "
        "'INSERT,UPDATE,DELETE')), false) AS can_write FROM unnest(%s::text[]) AS t "
        "WHERE to_regclass('public.' || t) IS NOT NULL", (KEY_TABLES,))[0]
    return row["role"], bool(row["can_write"])
