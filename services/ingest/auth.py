"""Per-agent bearer tokens.

The current design has no authentication at all: any host that can reach the
port can inject log records, and log data crosses the customer LAN in the clear.
Neither survives an audit, and the second one is worse than it sounds — logs
contain usernames, internal hostnames and paths.

Tokens are stored hashed. A stolen database dump should not be a working set of
agent credentials, and the tokens themselves are high-entropy random strings, so
plain SHA-256 is the right tool here rather than a password KDF: there is no
low-entropy secret to slow an attacker down against.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass

TOKEN_BYTES = 32
TOKEN_PREFIX = "awa_"  # anomaly-watch agent. Makes a leaked token greppable.

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id          BIGSERIAL PRIMARY KEY,
    agent_id    TEXT        NOT NULL UNIQUE,
    host        TEXT        NOT NULL,
    token_hash  TEXT        NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ,
    revoked_at  TIMESTAMPTZ
)
"""


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class _CacheEntry:
    agent_id: str | None  # None caches a rejection
    expires_at: float


class TokenStore:
    """Validates agent tokens against Postgres, with a short-lived cache.

    Rejections are cached too, and deliberately: without that, an agent stuck
    retrying with a revoked token turns every retry into a database query, so
    the misconfigured host becomes a load generator against the auth store.
    """

    def __init__(self, dsn: str, ttl_s: int = 60) -> None:
        self._dsn = dsn
        self._ttl = ttl_s
        self._cache: dict[str, _CacheEntry] = {}
        self._touched: dict[str, float] = {}

    def ensure_schema(self) -> None:
        import psycopg

        with psycopg.connect(self._dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(SCHEMA)
            connection.commit()

    def resolve(self, token: str) -> str | None:
        """Return the agent id for a valid token, or None. Never raises on a bad token."""
        digest = hash_token(token)
        now = time.monotonic()

        cached = self._cache.get(digest)
        if cached and cached.expires_at > now:
            return cached.agent_id

        agent_id = self._lookup(digest)
        self._cache[digest] = _CacheEntry(agent_id=agent_id, expires_at=now + self._ttl)
        return agent_id

    def _lookup(self, digest: str) -> str | None:
        import psycopg

        try:
            with psycopg.connect(self._dsn, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT agent_id FROM agents WHERE token_hash = %s AND revoked_at IS NULL",
                        (digest,),
                    )
                    row = cursor.fetchone()
        except Exception:  # noqa: BLE001
            # Auth store unreachable. Deny rather than fail open: an ingest
            # endpoint that accepts everything while Postgres is down is an open
            # relay for whoever notices first.
            return None
        return row[0] if row else None

    def touch(self, agent_id: str, min_interval_s: float = 60.0) -> None:
        """Best-effort last-seen update. An agent that stopped reporting is the
        thing an operator most needs to notice, and it is invisible without this.

        Throttled: an agent shipping batches every second must not turn into one
        write per second per agent against the same row.
        """
        import psycopg

        now = time.monotonic()
        if now - self._touched.get(agent_id, 0.0) < min_interval_s:
            return
        self._touched[agent_id] = now

        try:
            with psycopg.connect(self._dsn, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE agents SET last_seen = now() WHERE agent_id = %s", (agent_id,)
                    )
                connection.commit()
        except Exception:  # noqa: BLE001
            pass
