"""Smoke test for the bridge — delivery to the web app, without Postgres or the app.

The bridge is what an outage tests: the app goes down, the bridge restarts, a
report is rejected. Those are exercised here against a stand-in app that keeps
the parts of the contract the bridge relies on (duplicates by `anomalyId`,
windows replaced by minute, a 400 for a report that is wrong as a whole).

Covers:
  1. anomalies arrive with their own id and two distinct instants
  2. a restart does not resend, and a resend comes back DUPLICATE
  3. an unknown host is rejected once and not sent again
  4. a 400 holds the cursor until the cause is fixed
  5. an app that is down loses nothing
  6. reports stay under 500 anomalies and carry one model each
  7. latency windows carry the minute's total, and are resent when it grows

The database is stubbed: the stub answers the bridge's own statements from
lists in memory. The HTTP side is real, over a local socket.

Usage:
    uv run python tools/smoke_bridge.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services"))
sys.path.insert(0, str(REPO_ROOT / "services" / "bridge"))

# The bridge reads its configuration at import.
os.environ.update({"BRIDGE_DSN": "unused", "BRIDGE_APP_URL": "http://unused", "BRIDGE_PIPELINE_TOKEN": "secret"})

import bridge  # noqa: E402
from common.latency import BOUNDS_MS, BUCKETS, bucket_of  # noqa: E402


class App:
    """What the web app does with a report, reduced to what the bridge can observe."""

    def __init__(self) -> None:
        self.hosts = {"web1", "auth1"}
        self.anomalies: dict[str, dict] = {}
        self.windows: dict[tuple, dict] = {}
        self.reports: list[dict] = []  # every body received, in order
        self.broken = False  # answers 400 to everything, as a misconfigured report gets

    def handle(self, path: str, token: str, body: dict) -> tuple[int, dict]:
        if token != "Bearer secret":
            return 401, {}
        self.reports.append(body)
        if self.broken:
            return 400, {"detail": "El lote tiene que identificar el modelo (key y revision)."}
        if path == "/api/v1/pipeline/anomalies":
            if not body["model"]["revision"] or not 1 <= len(body["anomalies"]) <= 500:
                return 400, {"detail": "invalid batch"}
            results = []
            for anomaly in body["anomalies"]:
                uuid.UUID(anomaly["anomalyId"])
                if anomaly["anomalyId"] in self.anomalies:
                    outcome = {"outcome": "DUPLICATE"}
                elif anomaly["host"] not in self.hosts:
                    outcome = {"outcome": "REJECTED", "reason": "UNKNOWN_HOST", "detail": anomaly["host"]}
                elif anomaly["analyzedAt"] < anomaly["ingestedAt"]:
                    outcome = {"outcome": "REJECTED", "reason": "INVALID", "detail": "analyzedAt < ingestedAt"}
                else:
                    self.anomalies[anomaly["anomalyId"]] = anomaly | {"model": body["model"]}
                    outcome = {"outcome": "ACCEPTED"}
                results.append({"anomalyId": anomaly["anomalyId"], **outcome})
            count = lambda name: sum(r["outcome"] == name for r in results)  # noqa: E731
            return 200, {"accepted": count("ACCEPTED"), "duplicates": count("DUPLICATE"),
                         "rejected": count("REJECTED"), "alertsCreated": [], "results": results}
        if path == "/api/v1/pipeline/latency-windows":
            valid = body["boundsMs"] == list(BOUNDS_MS) and 1 <= len(body["windows"]) <= 1440 and all(
                len(w["bucketCounts"]) == BUCKETS and sum(w["bucketCounts"]) == w["count"] for w in body["windows"])
            if not valid:
                return 400, {"detail": "invalid windows"}
            for window in body["windows"]:
                # Replaced, not added: the same key twice keeps the last one.
                self.windows[(body["source"], body["instanceId"], window["stage"], window["windowStart"])] = window
            return 200, {"accepted": len(body["windows"])}
        return 404, {}


def serve(app: App) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            status, answer = app.handle(self.path, self.headers.get("Authorization", ""), body)
            payload = json.dumps(answer).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Rows:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchall(self) -> list:
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class Db:
    """The pipeline's Postgres, as far as the bridge's statements go."""

    def __init__(self) -> None:
        self.anomalies: list[tuple] = []
        self.latency: list[tuple] = []  # id, minute, count, sum_ms, max_ms, bucket_counts
        self.cursors: dict[str, int] = {}
        self._latency_id = 0

    def execute(self, statement: str, params: tuple = ()) -> Rows:
        if statement is bridge.READ_CURSOR:
            return Rows([(self.cursors[params[0]],)] if params[0] in self.cursors else [])
        if statement is bridge.WRITE_CURSOR:
            self.cursors[params[0]] = params[1]
            return Rows([])
        if statement is bridge.SELECT_ANOMALIES:
            return Rows([row for row in self.anomalies if row[0] > params[0]][: params[1]])
        if statement is bridge.SELECT_LATENCY_NEW:
            return Rows([row[:2] for row in self.latency if row[0] > params[0]][: params[1]])
        if statement is bridge.SELECT_LATENCY_MINUTES:
            return Rows([row[1:] for row in self.latency if row[1] in params[0]])
        if statement is bridge.DELETE_LATENCY:
            cutoff = datetime.now(UTC) - timedelta(hours=params[1])
            self.latency = [row for row in self.latency if not (row[0] <= params[0] and row[1] < cutoff)]
            return Rows([])
        raise AssertionError(f"unexpected statement: {statement}")

    def add_anomaly(self, host: str = "web1", revision: str = "rev-a", stamped: bool = True) -> tuple:
        row_id = len(self.anomalies) + 1
        ingested = datetime(2026, 1, 1, 14, 0, 0, tzinfo=UTC) + timedelta(seconds=row_id)
        row = (row_id, host, "sshd", f"Failed password for root from 10.0.0.{row_id}", None, None, None, 0.97,
               0.5, "modernbert", revision, ingested + timedelta(seconds=9),
               uuid.uuid4() if stamped else None,
               ingested if stamped else None,
               ingested + timedelta(seconds=4) if stamped else None)
        self.anomalies.append(row)
        return row

    def add_latency(self, minute: datetime, samples_ms: list[float]) -> None:
        buckets = [0] * BUCKETS
        for sample in samples_ms:
            buckets[bucket_of(sample)] += 1
        self._latency_id += 1  # a sequence: ids are not reused after a delete
        row = (self._latency_id, minute, len(samples_ms), float(sum(samples_ms)), float(max(samples_ms)), buckets)
        self.latency.append(row)


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    return bool(condition)


def main() -> int:
    failures = 0
    app, db = App(), Db()
    server = serve(app)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    bridge.APP_URL = url

    print("\n1. anomalies arrive with their id and instants")
    stamped = [db.add_anomaly() for _ in range(3)]
    legacy = db.add_anomaly(stamped=False)
    ghost = db.add_anomaly(host="ghost")
    bridge.forward_anomalies(db)
    sent = app.reports[-1]["anomalies"]
    failures += not check("the pipeline's anomaly_id is the one reported",
                          [a["anomalyId"] for a in sent[:3]] == [str(row[12]) for row in stamped])
    failures += not check("ingestedAt and analyzedAt are the row's, and differ",
                          all(a["ingestedAt"] == bridge.iso(row[13]) and a["analyzedAt"] == bridge.iso(row[14])
                              and a["analyzedAt"] > a["ingestedAt"] for a, row in zip(sent, stamped)),
                          f"{sent[0]['ingestedAt']} -> {sent[0]['analyzedAt']}")
    failures += not check("a row from before the change keeps its derived id and single instant",
                          sent[3]["anomalyId"] == str(uuid.uuid5(bridge.NAMESPACE, f"anomalies/{legacy[0]}"))
                          and sent[3]["ingestedAt"] == sent[3]["analyzedAt"] == bridge.iso(legacy[11]))
    failures += not check("the app holds the four it accepted", len(app.anomalies) == 4, str(len(app.anomalies)))
    failures += not check("the cursor is in the database, at the last row", db.cursors == {"anomalies": ghost[0]})

    print("\n2. restart and resend")
    seen = len(app.reports)
    bridge.forward_anomalies(db)
    failures += not check("a restarted bridge sends nothing it already confirmed", len(app.reports) == seen)
    # The app registered the report, the bridge died before writing the cursor.
    db.cursors["anomalies"] = 0
    bridge.forward_anomalies(db)
    failures += not check("resent rows are all duplicates or rejected again, none accepted twice",
                          len(app.reports) == seen + 1 and len(app.anomalies) == 4
                          and db.cursors["anomalies"] == ghost[0])

    print("\n3. unknown host")
    failures += not check("the row for an unknown host is not in the app", str(ghost[12]) not in app.anomalies)
    seen = len(app.reports)
    db.add_anomaly()
    bridge.forward_anomalies(db)
    failures += not check("and is not sent again with the next rows",
                          [a["host"] for a in app.reports[-1]["anomalies"]] == ["web1"])

    print("\n4. a report rejected whole")
    app.broken = True
    held = db.cursors["anomalies"]
    pending = [db.add_anomaly(), db.add_anomaly()]
    bridge.forward_anomalies(db)
    bridge.forward_anomalies(db)
    failures += not check("a 400 does not move the cursor", db.cursors["anomalies"] == held)
    failures += not check("nothing was skipped", all(str(row[12]) not in app.anomalies for row in pending))
    app.broken = False
    bridge.forward_anomalies(db)
    failures += not check("once fixed, the held rows are delivered",
                          all(str(row[12]) in app.anomalies for row in pending)
                          and db.cursors["anomalies"] == pending[-1][0])

    print("\n5. app down")
    held = db.cursors["anomalies"]
    pending = [db.add_anomaly() for _ in range(3)]
    server.shutdown()
    server.server_close()
    try:
        bridge.forward_anomalies(db)
        refused = False
    except OSError:
        refused = True  # main() catches this and retries
    failures += not check("a refused connection is an error the main loop retries", refused)
    failures += not check("the cursor stays", db.cursors["anomalies"] == held)
    server = serve(app)
    bridge.APP_URL = f"http://127.0.0.1:{server.server_address[1]}"
    bridge.forward_anomalies(db)
    failures += not check("when the app is back, nothing is missing",
                          all(str(row[12]) in app.anomalies for row in pending))

    print("\n6. report size and model")
    app.reports.clear()
    many = [db.add_anomaly() for _ in range(1200)]
    for _ in range(3):
        bridge.forward_anomalies(db)
    sizes = [len(r["anomalies"]) for r in app.reports]
    failures += not check("1200 pending go out in reports of at most 500", sizes == [500, 500, 200], str(sizes))
    failures += not check("all 1200 delivered", all(str(row[12]) in app.anomalies for row in many))
    app.reports.clear()
    db.add_anomaly(revision="rev-a"), db.add_anomaly(revision="rev-b"), db.add_anomaly(revision="rev-b")
    bridge.forward_anomalies(db)
    failures += not check("two revisions, two reports",
                          [(r["model"]["revision"], len(r["anomalies"])) for r in app.reports]
                          == [("rev-a", 1), ("rev-b", 2)])
    failures += not check("source and instance are fixed",
                          {(r["source"], r["instanceId"]) for r in app.reports} == {("pipeline", "pipeline")})

    print("\n7. latency windows")
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    first, second = now - timedelta(minutes=2), now - timedelta(minutes=1)
    window = lambda minute: app.windows[("pipeline", "pipeline", "ANALYSIS", minute.strftime("%Y-%m-%dT%H:%M:%SZ"))]  # noqa: E731
    app.reports.clear()
    db.add_latency(first, [40, 1000])      # one spool batch
    db.add_latency(first, [3000, 130000])  # another batch, same minute
    db.add_latency(second, [700])
    bridge.forward_latency(db)
    failures += not check("one window per minute", len(app.windows) == 2, str(len(app.windows)))
    failures += not check("a minute's rows are summed",
                          window(first)["count"] == 4 and window(first)["sumMs"] == 134040
                          and window(first)["maxMs"] == 130000 and window(first)["bucketCounts"][-1] == 1,
                          json.dumps(window(first)))
    failures += not check("bounds are declared", app.reports[-1]["boundsMs"] == list(BOUNDS_MS))

    seen = len(app.reports)
    bridge.forward_latency(db)
    failures += not check("nothing new, nothing sent", len(app.reports) == seen)

    db.add_latency(first, [200, 200])  # late samples in a minute already reported
    bridge.forward_latency(db)
    failures += not check("a minute with late samples is resent alone, with the total",
                          [w["windowStart"] for w in app.reports[-1]["windows"]]
                          == [first.strftime("%Y-%m-%dT%H:%M:%SZ")] and window(first)["count"] == 6,
                          str(window(first)["count"]))

    db.cursors["latency"] = 0  # the bridge restarted before writing its cursor
    bridge.forward_latency(db)
    failures += not check("a resend leaves one window per minute, same samples",
                          len(app.windows) == 2 and window(first)["count"] == 6 and window(second)["count"] == 1)

    app.broken = True
    held = db.cursors["latency"]
    db.add_latency(second, [900])
    bridge.forward_latency(db)
    failures += not check("a 400 does not move the latency cursor", db.cursors["latency"] == held)
    app.broken = False
    bridge.forward_latency(db)
    failures += not check("and the window arrives once fixed", window(second)["count"] == 2)

    old = now - timedelta(hours=2)
    kept = len(db.latency)
    db.add_latency(old, [100])
    bridge.forward_latency(db)
    failures += not check("a row older than an hour is deleted once sent, recent ones kept",
                          old.strftime("%Y-%m-%dT%H:%M:%SZ") in app.reports[-1]["windows"][0]["windowStart"]
                          and all(row[1] != old for row in db.latency) and len(db.latency) == kept,
                          f"{len(db.latency)} rows left")

    app.reports.clear()
    for offset in range(1500):
        db.add_latency(now - timedelta(days=3, minutes=offset), [100])
    bridge.forward_latency(db)
    sizes = [len(r["windows"]) for r in app.reports]
    failures += not check("1500 windows go out in reports of at most 1440", sizes == [1440, 60], str(sizes))

    server.shutdown()
    print()
    if failures:
        print(f"FAIL: {failures} check(s) failed")
        return 1
    print("PASS: the bridge delivers, holds and resends")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
