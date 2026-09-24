"""latency-reporting: stamping, per-minute histograms and the bounded retry buffer."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from latency import BOUNDS_MS, LatencyWindow, Reporter, Stamp, WindowAggregator
from parser import LogParser
from replay import reporting_config_errors

T0 = datetime(2026, 9, 24, 14, 5, 12, tzinfo=UTC)


# --- stamping ---------------------------------------------------------------

def test_duration_uses_the_monotonic_clock_even_if_the_wall_clock_goes_back():
    ingested = Stamp(wall=T0, mono=100.0)
    analyzed = Stamp(wall=T0 - timedelta(minutes=5), mono=100.250)

    assert ingested.elapsed_ms_until(analyzed) == pytest.approx(250.0)


def test_duration_is_never_negative():
    assert Stamp(T0, 100.0).elapsed_ms_until(Stamp(T0, 99.0)) == 0.0


def test_scoring_payload_carries_no_timing_fields():
    record = LogParser().parse("081109 203615 148 INFO dfs.DataNode$PacketResponder: Received block")

    assert set(record.to_payload()) <= {"message", "template_id", "template", "params", "host", "timestamp"}


# --- histograms -------------------------------------------------------------

@pytest.mark.parametrize(
    ("duration_ms", "bucket"),
    [(0, 0), (10, 0), (10.1, 1), (60000, len(BOUNDS_MS) - 1), (60000.1, len(BOUNDS_MS)), (10**9, len(BOUNDS_MS))],
)
def test_a_value_on_a_bound_lands_in_that_bounds_bucket(duration_ms, bucket):
    window = LatencyWindow(T0)
    window.add(duration_ms)

    assert window.bucket_counts[bucket] == 1


def test_window_counts_add_up_and_have_one_bucket_per_bound_plus_the_open_one():
    window = LatencyWindow(T0)
    for duration_ms in (5, 80, 80, 700, 90000):
        window.add(duration_ms)

    payload = window.to_payload()
    assert len(payload["bucketCounts"]) == len(BOUNDS_MS) + 1
    assert sum(payload["bucketCounts"]) == payload["count"] == 5
    assert payload["maxMs"] == 90000
    assert payload["stage"] == "ANALYSIS"


def test_samples_are_split_by_the_utc_minute_of_their_analysis():
    aggregator = WindowAggregator()
    aggregator.add(100, T0)
    aggregator.add(100, T0 + timedelta(seconds=30))
    aggregator.add(100, T0 + timedelta(minutes=1))

    windows = aggregator.close_all()

    assert [w.to_payload()["windowStart"] for w in windows] == ["2026-09-24T14:05:00Z", "2026-09-24T14:06:00Z"]
    assert [w.count for w in windows] == [2, 1]


def test_a_quiet_minute_is_closed_without_waiting_for_another_sample():
    aggregator = WindowAggregator()
    aggregator.add(100, T0)

    assert aggregator.close_due(T0) == []
    closed = aggregator.close_due(T0 + timedelta(minutes=3))

    assert [w.count for w in closed] == [1]
    assert aggregator.close_all() == []


# --- reporting --------------------------------------------------------------

class FakeAnomalyWatch:
    """httpx MockTransport handler that records payloads and plays a status script."""

    def __init__(self, *statuses: int | Exception):
        self.statuses = list(statuses)
        self.payloads: list[dict] = []
        self.headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        outcome = self.statuses.pop(0) if self.statuses else 200
        if isinstance(outcome, Exception):
            raise outcome
        self.payloads.append(json.loads(request.content))
        self.headers.append(request.headers)
        return httpx.Response(outcome, json={"accepted": 0}, request=request)


def make_reporter(server: FakeAnomalyWatch, buffer_size: int = 60, log=None):
    client = httpx.Client(transport=httpx.MockTransport(server))
    return Reporter(client, "http://aw.test", "secret", "replay", "replay-1",
                    buffer_size=buffer_size, log=log or (lambda _: None))


def window_at(minute: int) -> LatencyWindow:
    window = LatencyWindow(datetime(2026, 9, 24, 14, minute, tzinfo=UTC))
    window.add(100)
    return window


def test_a_successful_report_sends_the_contract_and_clears_the_buffer():
    server = FakeAnomalyWatch(200)
    reporter = make_reporter(server)

    reporter.enqueue([window_at(5)])
    reporter.flush()

    payload = server.payloads[0]
    assert payload["source"] == "replay"
    assert payload["instanceId"] == "replay-1"
    assert payload["boundsMs"] == list(BOUNDS_MS)
    assert server.headers[0]["authorization"] == "Bearer secret"
    assert (reporter.delivered, reporter.pending) == (1, 0)


def test_windows_pending_during_an_outage_are_delivered_once_after_recovery():
    server = FakeAnomalyWatch(httpx.ConnectError("down"), 503, httpx.ReadTimeout("slow"), 200)
    reporter = make_reporter(server)

    for minute in (5, 6, 7, 8):
        reporter.enqueue([window_at(minute)])
        reporter.flush()

    delivered = server.payloads[-1]["windows"]
    assert [w["windowStart"][14:16] for w in delivered] == ["05", "06", "07", "08"]
    assert (reporter.delivered, reporter.pending, reporter.dropped) == (4, 0, 0)


def test_an_outage_longer_than_the_buffer_drops_the_oldest_and_counts_them():
    messages = []
    reporter = make_reporter(FakeAnomalyWatch(*[httpx.ConnectError("down")] * 10), buffer_size=3,
                             log=messages.append)

    for minute in range(5):
        reporter.enqueue([window_at(minute)])
        reporter.flush()

    assert reporter.pending == 3
    assert reporter.dropped == 2
    assert any("dropped" in message for message in messages)


def test_a_rejected_report_is_logged_and_not_retried_unchanged():
    messages = []
    server = FakeAnomalyWatch(400, 200)
    reporter = make_reporter(server, log=messages.append)

    reporter.enqueue([window_at(5)])
    reporter.flush()
    reporter.flush()

    assert len(server.payloads) == 1
    assert (reporter.rejected, reporter.pending) == (1, 0)
    assert any("400" in message for message in messages)


def test_flush_never_raises():
    reporter = make_reporter(FakeAnomalyWatch(RuntimeError("unexpected")))
    reporter.enqueue([window_at(5)])

    reporter.flush()

    assert reporter.pending == 1


# --- configuration ----------------------------------------------------------

def test_reporting_off_needs_nothing():
    assert reporting_config_errors(False, None, None) == []


@pytest.mark.parametrize(("endpoint", "token", "missing"), [
    (None, "t", "--aw-endpoint"),
    ("http://aw", None, "PIPELINE_SERVICE_TOKEN"),
])
def test_reporting_without_endpoint_or_token_names_what_is_missing(endpoint, token, missing):
    errors = reporting_config_errors(True, endpoint, token)

    assert len(errors) == 1
    assert missing in errors[0]
