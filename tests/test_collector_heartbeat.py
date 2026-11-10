"""Node heartbeat reporter (services/collector/heartbeat.py).

No database and no network: the reporter takes its reader and its HTTP call
as functions, so these tests hand it fakes.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "collector"))

from heartbeat import (  # noqa: E402
    MAX_NODES_PER_HEARTBEAT,
    HeartbeatReporter,
    HostSighting,
    build_payloads,
    summarize_rejections,
    to_iso_utc,
)

NOW = datetime(2026, 11, 9, 22, 14, 3, tzinfo=timezone.utc)


def sighting(host: str, minutes_ago: int = 0) -> HostSighting:
    return HostSighting(host=host, last_seen=NOW - timedelta(minutes=minutes_ago))


class FakePost:
    def __init__(self, response: dict | None = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict, dict]] = []
        self._response = response or {"collector": {}, "nodes": []}
        self._error = error

    def __call__(self, url: str, body: dict, headers: dict) -> dict:
        self.calls.append((url, body, headers))
        if self._error is not None:
            raise self._error
        return self._response


def test_iso_utc_has_a_z_and_treats_naive_as_utc():
    assert to_iso_utc(NOW) == "2026-11-09T22:14:03Z"
    assert to_iso_utc(NOW.replace(tzinfo=None)) == "2026-11-09T22:14:03Z"
    assert to_iso_utc(NOW.astimezone(timezone(timedelta(hours=-3)))) == "2026-11-09T22:14:03Z"


def test_payload_sends_last_seen_per_host_not_the_sending_time():
    [body] = build_payloads([sighting("web-01", 2), sighting("db-01", 10)])

    assert body == {
        "nodes": [
            {"host": "db-01", "lastSeenAt": "2026-11-09T22:04:03Z"},
            {"host": "web-01", "lastSeenAt": "2026-11-09T22:12:03Z"},
        ]
    }


def test_without_hosts_the_collector_still_reports():
    assert build_payloads([]) == [{"nodes": []}]


def test_large_fleets_are_split_under_the_backend_ceiling():
    bodies = build_payloads([sighting(f"host-{i:04d}") for i in range(MAX_NODES_PER_HEARTBEAT + 1)])

    assert [len(b["nodes"]) for b in bodies] == [MAX_NODES_PER_HEARTBEAT, 1]


def test_rejections_are_grouped_by_outcome():
    responses = [
        {"nodes": [
            {"host": "web-01", "outcome": "ACCEPTED"},
            {"host": "ghost", "outcome": "UNKNOWN_HOST"},
            {"host": "other", "outcome": "FOREIGN_NODE"},
        ]},
        {"nodes": [{"host": "alpha", "outcome": "UNKNOWN_HOST"}]},
    ]

    assert summarize_rejections(responses) == {
        "UNKNOWN_HOST": ["alpha", "ghost"],
        "FOREIGN_NODE": ["other"],
    }


def test_tick_posts_to_the_collector_heartbeat_with_its_credential():
    post = FakePost({"collector": {}, "nodes": [{"host": "web-01", "outcome": "ACCEPTED"}]})
    reporter = HeartbeatReporter("http://aw:8080/", "agent-01", "awc_secret", read=lambda: [sighting("web-01")], post=post)

    reporter.tick()

    [(url, body, headers)] = post.calls
    assert url == "http://aw:8080/api/v1/collectors/agent-01/heartbeat"
    assert headers == {"Authorization": "Bearer awc_secret"}
    assert body["nodes"][0]["host"] == "web-01"
    assert reporter.state.hosts_reported == 1
    assert reporter.state.last_error is None
    assert reporter.state.last_sent_at is not None
    assert reporter.state.rejected == {}


@pytest.mark.parametrize("failing", ["read", "post"])
def test_a_failure_is_recorded_and_never_raised(failing: str):
    def broken_read():
        raise ConnectionError("postgres down")

    post = FakePost(error=ConnectionError("backend down")) if failing == "post" else FakePost()
    read = broken_read if failing == "read" else (lambda: [sighting("web-01")])
    reporter = HeartbeatReporter("http://aw", "agent-01", "awc_secret", read=read, post=post)

    reporter.tick()

    assert reporter.state.last_error is not None
    assert "awc_secret" not in reporter.state.last_error
    assert reporter.state.last_sent_at is None


def test_a_later_success_clears_the_error():
    calls = {"n": 0}

    def flaky_read():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("postgres down")
        return [sighting("web-01")]

    reporter = HeartbeatReporter("http://aw", "agent-01", "t", read=flaky_read, post=FakePost())

    reporter.tick()
    reporter.tick()

    assert reporter.state.last_error is None
    assert reporter.state.hosts_reported == 1
