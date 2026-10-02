"""anomaly-reporting: anomaly identity, the bounded retry buffer and the wiring in replay.py."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

import httpx
import pytest
import replay
from anomaly_reporting import AnomalyReporter, Model, to_payload
from replay import assign_anomaly_ids, reporting_config_errors

T0 = datetime(2026, 9, 24, 14, 5, 12, 250000, tzinfo=UTC)
T1 = datetime(2026, 9, 24, 14, 5, 13, 500000, tzinfo=UTC)
MODEL = Model("modernbert-ft", "abc123", 0.5)
OTHER_MODEL = Model("modernbert-ft", "def456", 0.5)


def scored_row(message: str = "Exception in receiveBlock", is_anomaly: bool = True, score: float = 0.9) -> dict:
    return {
        "message": message,
        "template_id": 7,
        "template": "Exception in <*>",
        "params": ["receiveBlock"],
        "host": "web-front-01.prod.internal",
        "timestamp": "081109 203615",
        "score": score,
        "is_anomaly": is_anomaly,
        "ingested_at": T0,
        "analyzed_at": T1,
    }


def flagged_rows(count: int) -> list[dict]:
    return assign_anomaly_ids([scored_row(f"line {index}") for index in range(count)])


# --- anomaly identity -------------------------------------------------------

def test_each_flagged_row_gets_its_own_id_even_for_identical_lines():
    rows = [scored_row(), scored_row(), scored_row(is_anomaly=False, score=0.1)]

    flagged = assign_anomaly_ids(rows)

    assert flagged == rows[:2]
    assert rows[0]["anomaly_id"] != rows[1]["anomaly_id"]
    assert "anomaly_id" not in rows[2]


def test_the_jsonl_row_and_the_reported_anomaly_carry_the_same_id():
    [row] = assign_anomaly_ids([scored_row()])

    written = json.loads(json.dumps(row, default=replay._json_default))

    assert written["anomaly_id"] == to_payload(row, "HDFS_2k.log")["anomalyId"] == row["anomaly_id"]


def test_payload_follows_the_contract():
    [row] = assign_anomaly_ids([scored_row()])

    payload = to_payload(row, "HDFS_2k.log")

    assert payload == {
        "anomalyId": row["anomaly_id"],
        "host": "web-front-01.prod.internal",
        "logSource": "HDFS_2k.log",
        "message": "Exception in receiveBlock",
        "templateId": "7",
        "template": "Exception in <*>",
        "logTimestamp": "081109 203615",
        "score": 0.9,
        "ingestedAt": "2026-09-24T14:05:12.250000Z",
        "analyzedAt": "2026-09-24T14:05:13.500000Z",
    }


# --- reporter ---------------------------------------------------------------

class FakeAnomalyWatch:
    """MockTransport handler that answers like POST /pipeline/anomalies, playing a status script.

    By default every anomaly is ACCEPTED, unless its id was already registered
    (DUPLICATE) or `reject` names a reason for all of them.
    """

    def __init__(self, *statuses: int | Exception, reject: str | None = None, alerts: list[str] | None = None):
        self.statuses = list(statuses)
        self.reject = reject
        self.alerts = alerts or []
        self.payloads: list[dict] = []
        self.headers: list[httpx.Headers] = []
        self.registered: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        outcome = self.statuses.pop(0) if self.statuses else 200
        if isinstance(outcome, Exception):
            raise outcome
        payload = json.loads(request.content)
        self.payloads.append(payload)
        self.headers.append(request.headers)
        if outcome != 200:
            return httpx.Response(outcome, text="bad batch", request=request)

        results = []
        for anomaly in payload["anomalies"]:
            anomaly_id = anomaly["anomalyId"]
            if self.reject:
                results.append({"anomalyId": anomaly_id, "outcome": "REJECTED", "reason": self.reject})
            elif anomaly_id in self.registered:
                results.append({"anomalyId": anomaly_id, "outcome": "DUPLICATE"})
            else:
                self.registered.add(anomaly_id)
                results.append({"anomalyId": anomaly_id, "outcome": "ACCEPTED"})
        return httpx.Response(200, json={"results": results, "alertsCreated": self.alerts}, request=request)

    @property
    def sent_ids(self) -> list[str]:
        return [anomaly["anomalyId"] for payload in self.payloads for anomaly in payload["anomalies"]]


def make_reporter(server: FakeAnomalyWatch, buffer_size: int = 5000, log=None) -> AnomalyReporter:
    client = httpx.Client(transport=httpx.MockTransport(server))
    return AnomalyReporter(client, "http://aw.test", "secret", "replay", "replay-1",
                           buffer_size=buffer_size, log=log or (lambda _: None), log_source="HDFS_2k.log")


def test_a_successful_report_sends_the_contract_and_clears_the_buffer():
    server = FakeAnomalyWatch(alerts=["ALT-1001"])
    reporter = make_reporter(server)

    reporter.enqueue(flagged_rows(2), MODEL)
    reporter.flush()

    payload = server.payloads[0]
    assert server.headers[0]["authorization"] == "Bearer secret"
    assert (payload["source"], payload["instanceId"]) == ("replay", "replay-1")
    assert payload["model"] == {"key": "modernbert-ft", "revision": "abc123", "threshold": 0.5}
    assert len(payload["anomalies"]) == 2
    assert (reporter.accepted, reporter.pending) == (2, 0)
    assert reporter.alerts_created == ["ALT-1001"]


def test_anomalies_pending_during_an_outage_are_delivered_once_after_recovery():
    server = FakeAnomalyWatch(httpx.ConnectError("down"), 503, httpx.ReadTimeout("slow"), 200)
    reporter = make_reporter(server)
    rows = flagged_rows(4)

    for row in rows:
        reporter.enqueue([row], MODEL)
        reporter.flush()

    assert server.payloads[-1]["anomalies"] == [to_payload(row, "HDFS_2k.log") for row in rows]
    assert server.registered == {row["anomaly_id"] for row in rows}
    assert (reporter.accepted, reporter.pending, reporter.dropped) == (4, 0, 0)


def test_an_outage_longer_than_the_buffer_drops_the_oldest_and_counts_them():
    messages = []
    reporter = make_reporter(FakeAnomalyWatch(*[httpx.ConnectError("down")] * 10), buffer_size=3,
                             log=messages.append)

    for row in flagged_rows(5):
        reporter.enqueue([row], MODEL)
        reporter.flush()

    assert (reporter.pending, reporter.dropped) == (3, 2)
    assert any("dropped" in message for message in messages)
    assert "2 dropped" in reporter.summary() and "3 undelivered" in reporter.summary()


def test_a_rejected_batch_is_logged_and_not_retried():
    messages = []
    server = FakeAnomalyWatch(400, 200)
    reporter = make_reporter(server, log=messages.append)

    reporter.enqueue(flagged_rows(2), MODEL)
    reporter.flush()
    reporter.flush()

    assert len(server.payloads) == 1
    assert (reporter.discarded, reporter.pending) == (2, 0)
    assert any("400" in message for message in messages)


def test_a_large_backlog_goes_in_requests_of_at_most_500():
    server = FakeAnomalyWatch()
    reporter = make_reporter(server)

    reporter.enqueue(flagged_rows(1200), MODEL)
    reporter.flush()

    assert [len(payload["anomalies"]) for payload in server.payloads] == [500, 500, 200]
    assert (reporter.accepted, reporter.pending) == (1200, 0)


def test_anomalies_of_different_models_go_in_separate_requests():
    server = FakeAnomalyWatch()
    reporter = make_reporter(server)

    reporter.enqueue(flagged_rows(2), MODEL)
    reporter.enqueue(flagged_rows(3), OTHER_MODEL)
    reporter.flush()

    assert [(payload["model"]["revision"], len(payload["anomalies"])) for payload in server.payloads] == [
        ("abc123", 2), ("def456", 3)]


def test_unknown_host_rejections_are_counted_and_not_resent():
    server = FakeAnomalyWatch(reject="UNKNOWN_HOST")
    reporter = make_reporter(server)

    reporter.enqueue(flagged_rows(3), MODEL)
    reporter.flush()
    reporter.flush()

    assert len(server.payloads) == 1
    assert reporter.rejected == {"UNKNOWN_HOST": 3}
    assert reporter.pending == 0
    assert "3 rejected (UNKNOWN_HOST 3)" in reporter.summary()


def test_a_resend_answered_as_duplicates_counts_as_duplicates():
    # The first report registered the anomalies but timed out on the way back.
    server = FakeAnomalyWatch()
    rows = flagged_rows(2)
    server.registered = {row["anomaly_id"] for row in rows}
    reporter = make_reporter(server)

    reporter.enqueue(rows, MODEL)
    reporter.flush()

    assert (reporter.accepted, reporter.duplicates, reporter.pending) == (0, 2, 0)


def test_an_outage_stops_the_flush_instead_of_timing_out_every_request():
    server = FakeAnomalyWatch(*[httpx.ConnectError("down")] * 5)
    reporter = make_reporter(server)

    reporter.enqueue(flagged_rows(1200), MODEL)
    reporter.flush()

    assert len(server.statuses) == 4
    assert reporter.pending == 1200


def test_flush_never_raises():
    reporter = make_reporter(FakeAnomalyWatch(RuntimeError("unexpected")))
    reporter.enqueue(flagged_rows(1), MODEL)

    reporter.flush()

    assert reporter.pending == 1


# --- configuration ----------------------------------------------------------

def test_anomaly_reporting_off_needs_nothing():
    assert reporting_config_errors(False, None, None, report_anomalies=False, host=None) == []


@pytest.mark.parametrize(("endpoint", "token", "host", "missing"), [
    (None, "t", "web-front-01", "--aw-endpoint"),
    ("http://aw", None, "web-front-01", "PIPELINE_SERVICE_TOKEN"),
    ("http://aw", "t", None, "--host"),
])
def test_anomaly_reporting_names_what_is_missing(endpoint, token, host, missing):
    errors = reporting_config_errors(False, endpoint, token, report_anomalies=True, host=host)

    assert len(errors) == 1
    assert missing in errors[0]
    assert "--report-anomalies" in errors[0]


def test_both_reports_share_one_message_per_missing_setting():
    errors = reporting_config_errors(True, None, "t", report_anomalies=True, host="web-front-01")

    assert errors == ["--aw-endpoint is required with --report-metrics and --report-anomalies"]


# --- wiring -----------------------------------------------------------------

LOG_LINES = [
    "081109 203615 148 INFO dfs.DataNode$PacketResponder: PacketResponder 1 for block blk_38865049064139660 terminating",
    "081109 203807 222 INFO dfs.DataNode$PacketResponder: PacketResponder 0 for block blk_-6952295868487656571 terminating",
    "081109 204005 35 WARN dfs.DataNode$DataXceiver: Exception in receiveBlock for block blk_-1608999687919862906",
    "081109 204015 308 INFO dfs.DataNode$PacketResponder: PacketResponder 2 for block blk_8229193803249955061 terminating",
]


class Stack:
    """MockTransport for detector-service and AnomalyWatch together: flags lines with an exception."""

    def __init__(self, anomaly_watch: FakeAnomalyWatch):
        self.anomaly_watch = anomaly_watch
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if request.url.path == "/model":
            return httpx.Response(200, json={"repo_id": "test/model", "revision": "abc123def456", "threshold": 0.5})
        if request.url.path == "/score":
            records = json.loads(request.content)["records"]
            results = [{"score": 0.97 if "Exception" in r["message"] else 0.02,
                        "is_anomaly": "Exception" in r["message"]} for r in records]
            return httpx.Response(200, json={"threshold": 0.5, "model_key": "test-model",
                                             "model_revision": "abc123", "results": results})
        if request.url.path == "/api/v1/pipeline/anomalies":
            return self.anomaly_watch(request)
        return httpx.Response(200, json={}, request=request)


@pytest.fixture
def run_replay(tmp_path, monkeypatch):
    real_client = httpx.Client

    def run(*flags: str, anomaly_watch: FakeAnomalyWatch | None = None, token: str | None = "secret"):
        logfile = tmp_path / "sample.log"
        logfile.write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
        out = tmp_path / "scored.jsonl"
        stack = Stack(anomaly_watch or FakeAnomalyWatch())
        monkeypatch.setattr(httpx, "Client", lambda *a, **k: real_client(transport=httpx.MockTransport(stack)))
        if token is None:
            monkeypatch.delenv(replay.TOKEN_VARIABLE, raising=False)
        else:
            monkeypatch.setenv(replay.TOKEN_VARIABLE, token)
        monkeypatch.setattr(sys, "argv", ["replay.py", str(logfile), "--out", str(out), "--show", "0", *flags])
        exit_code = replay.main()
        rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()] if out.exists() else []
        return exit_code, stack, rows

    return run


def test_without_the_flag_no_anomaly_is_reported(run_replay):
    exit_code, stack, rows = run_replay()

    assert exit_code == 0
    assert "/api/v1/pipeline/anomalies" not in stack.paths
    assert sum(row["is_anomaly"] for row in rows) == 1


def test_with_the_flag_only_flagged_lines_are_reported_with_the_jsonl_id(run_replay, capsys):
    exit_code, stack, rows = run_replay("--report-anomalies", "--host", "web-front-01.prod.internal",
                                        "--aw-endpoint", "http://aw.test")

    flagged = [row for row in rows if row["is_anomaly"]]
    [payload] = stack.anomaly_watch.payloads
    assert exit_code == 0
    assert [anomaly["anomalyId"] for anomaly in payload["anomalies"]] == [row["anomaly_id"] for row in flagged]
    assert payload["anomalies"][0]["logSource"] == "sample.log"
    assert payload["model"] == {"key": "test-model", "revision": "abc123", "threshold": 0.5}
    assert all("anomaly_id" not in row for row in rows if not row["is_anomaly"])
    assert "reported : 1 accepted" in capsys.readouterr().out


def test_an_unreachable_anomaly_watch_does_not_change_the_exit_code(run_replay, capsys):
    exit_code, _, rows = run_replay("--report-anomalies", "--host", "web-front-01.prod.internal",
                                    "--aw-endpoint", "http://aw.test",
                                    anomaly_watch=FakeAnomalyWatch(*[httpx.ConnectError("down")] * 10))

    assert exit_code == 0
    assert len(rows) == len(LOG_LINES)
    assert "1 undelivered" in capsys.readouterr().out


@pytest.mark.parametrize(("flags", "token", "missing"), [
    (["--aw-endpoint", "http://aw.test"], "secret", "--host"),
    (["--host", "web-front-01.prod.internal"], "secret", "--aw-endpoint"),
    (["--host", "web-front-01.prod.internal", "--aw-endpoint", "http://aw.test"], None, "PIPELINE_SERVICE_TOKEN"),
])
def test_missing_configuration_aborts_before_reading_any_line(run_replay, capsys, flags, token, missing):
    exit_code, stack, rows = run_replay("--report-anomalies", *flags, token=token)

    assert exit_code == 2
    assert stack.paths == []
    assert rows == []
    assert missing in capsys.readouterr().err
