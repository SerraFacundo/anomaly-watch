"""Node heartbeats to AnomalyWatch.

`ingest-service` already knows when each host last shipped a batch: it stamps
`agents.last_seen` (throttled to once a minute per agent). This module turns
that into the AnomalyWatch collector heartbeat, which carries one entry per
host, so the platform can tell a host that went quiet from one that is fine.

What travels is `lastSeenAt`, the instant the host was last heard from, not
the time of sending. A host silent for ten minutes must not look alive just
because this collector keeps reporting.

Contract (owned by AnomalyWatch, change add-node-heartbeat-and-alert-risk):

    POST {backend}/api/v1/collectors/{code}/heartbeat
    Authorization: Bearer <collector agent credential>
    {"nodes": [{"host": "web-01", "lastSeenAt": "2026-11-09T22:14:03Z"}]}

The reporter never raises into the pipeline. A backend that is down costs a
log line and a retry on the next tick, nothing else: detection does not depend
on it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

logger = logging.getLogger("collector.heartbeat")

# The backend rejects a heartbeat with more nodes than this, whole. Larger
# fleets go out in several heartbeats in the same tick.
MAX_NODES_PER_HEARTBEAT = 1000

# One row per host: an agent re-enrolled with a new token is a second row for
# the same host, and the most recent sighting is the one that counts.
LAST_SEEN_QUERY = """
SELECT host, MAX(last_seen)
FROM agents
WHERE revoked_at IS NULL AND last_seen IS NOT NULL
GROUP BY host
"""

# Outcomes the backend reports per node. Anything but ACCEPTED is a host the
# operator has to fix on one side or the other.
ACCEPTED = "ACCEPTED"


@dataclass(frozen=True)
class HostSighting:
    host: str
    last_seen: datetime


@dataclass
class HeartbeatState:
    """What `/status` shows. Enough to tell "not configured" from "failing"."""

    enabled: bool
    last_sent_at: float | None = None
    last_error: str | None = None
    hosts_reported: int = 0
    rejected: dict[str, list[str]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "last_sent_at": self.last_sent_at,
            "last_error": self.last_error,
            "hosts_reported": self.hosts_reported,
            "rejected": self.rejected,
        }


def to_iso_utc(instant: datetime) -> str:
    """ISO 8601 in UTC with a `Z`. A naive value is taken as UTC, which is what
    Postgres hands back for `timestamptz` read without a session zone."""
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_payloads(sightings: Iterable[HostSighting]) -> list[dict]:
    """Heartbeat bodies, each within the backend's node ceiling.

    Always at least one body: with no hosts yet the collector still reports
    that it is alive.
    """
    nodes = [
        {"host": s.host, "lastSeenAt": to_iso_utc(s.last_seen)}
        for s in sorted(sightings, key=lambda s: s.host)
    ]
    if not nodes:
        return [{"nodes": []}]
    return [
        {"nodes": nodes[i : i + MAX_NODES_PER_HEARTBEAT]}
        for i in range(0, len(nodes), MAX_NODES_PER_HEARTBEAT)
    ]


def summarize_rejections(responses: Iterable[dict]) -> dict[str, list[str]]:
    """Hosts the backend did not accept, grouped by outcome."""
    rejected: dict[str, list[str]] = {}
    for response in responses:
        for node in response.get("nodes") or []:
            outcome = node.get("outcome")
            if outcome and outcome != ACCEPTED:
                rejected.setdefault(outcome, []).append(str(node.get("host")))
    return {outcome: sorted(hosts) for outcome, hosts in rejected.items()}


def read_sightings(dsn: str) -> list[HostSighting]:
    """Last sighting per host from the ingest agents table."""
    import psycopg

    with psycopg.connect(dsn, connect_timeout=5) as connection:
        with connection.cursor() as cursor:
            cursor.execute(LAST_SEEN_QUERY)
            return [HostSighting(host=row[0], last_seen=row[1]) for row in cursor.fetchall()]


Poster = Callable[[str, dict, dict], dict]


def make_poster(timeout_s: float = 10.0) -> Poster:
    """HTTP POST returning the decoded JSON body; raises on a non-2xx status."""
    import httpx

    client = httpx.Client(timeout=timeout_s)

    def post(url: str, body: dict, headers: dict) -> dict:
        response = client.post(url, json=body, headers=headers)
        response.raise_for_status()
        return response.json()

    return post


class HeartbeatReporter:
    """Sends one heartbeat round per tick. Synchronous; the app runs it in a thread."""

    def __init__(
        self,
        backend_url: str,
        collector_code: str,
        token: str,
        read: Callable[[], list[HostSighting]],
        post: Poster,
    ) -> None:
        self._url = f"{backend_url.rstrip('/')}/api/v1/collectors/{collector_code}/heartbeat"
        self._headers = {"Authorization": f"Bearer {token}"}
        self._read = read
        self._post = post
        self.state = HeartbeatState(enabled=True)

    def tick(self) -> None:
        try:
            sightings = self._read()
            responses = [self._post(self._url, body, self._headers) for body in build_payloads(sightings)]
        except Exception as exc:  # noqa: BLE001
            # Never let a reporting problem reach the pipeline. The message is
            # kept for /status; the token never appears in it.
            self.state.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Node heartbeat failed; retrying next tick: %s", self.state.last_error)
            return

        rejected = summarize_rejections(responses)
        if rejected != self.state.rejected and rejected:
            # Logged when it changes, not every minute: an unregistered host
            # would otherwise drown the log.
            logger.warning("AnomalyWatch did not accept some hosts: %s", rejected)

        self.state.rejected = rejected
        self.state.hosts_reported = len(sightings)
        self.state.last_sent_at = time.time()
        self.state.last_error = None
