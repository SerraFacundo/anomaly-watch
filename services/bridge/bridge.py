"""Bridge from the pipeline to the web app.

Three loops, one process:

- Anomalies. New rows of `anomalies` go to the app's
  `POST /api/v1/pipeline/anomalies`, at most 500 per batch, authenticated with
  the shared pipeline token. The app opens and groups the alerts.
- Latency. Every minute that gained analysis samples goes to
  `POST /api/v1/pipeline/latency-windows` with that minute's total. The app
  replaces a window it already has, so a minute is always sent whole.
- Heartbeats. Every agent whose `last_seen` is recent gets its collector's
  `POST /api/v1/collectors/{code}/heartbeat`, with that collector's own
  credential. That is what puts its node online in the app: "connected" means
  the agent is delivering logs, not that some process is alive.

Polls Postgres rather than hooking the collector: the hot path stays untouched,
and an app that is down or slow costs this process retries, never the pipeline
a stall. Each loop keeps a cursor, the last row id the app acknowledged, in the
same database as the rows it counts. Kept anywhere else, recreating the database
alone leaves the cursor above every new id and nothing is forwarded, silently.

Delivery is at least once. Re-sending is harmless: an `anomalyId` the app knows
comes back as DUPLICATE, and a latency window replaces itself.

Anything but a 200 leaves the cursor where it was. That includes a 400: it means
the report itself is wrong (empty model revision, bucket bounds that moved), so
every later report fails the same way, and skipping would discard them all.

Known ceiling: a collapsed row (same template, host and hour) is forwarded once,
when it first appears. The occurrences added to it later stay in the pipeline;
the app sees the first one, which is what opens the alert.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from datetime import UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.latency import BOUNDS_MS, BUCKETS  # noqa: E402

DSN = os.environ["BRIDGE_DSN"]
APP_URL = os.environ["BRIDGE_APP_URL"].rstrip("/")
PIPELINE_TOKEN = os.environ["BRIDGE_PIPELINE_TOKEN"]
CREDENTIALS = Path(os.environ.get("BRIDGE_CREDENTIALS", "/creds/app-credentials.env"))
MODEL_REVISION = os.environ.get("BRIDGE_MODEL_REVISION", "")
# Fixed across restarts. The app keys a latency window by source, instance,
# stage and minute, so a fresh id per start would record the same minute twice.
INSTANCE_ID = os.environ.get("BRIDGE_INSTANCE_ID") or "pipeline"
POLL_SECONDS = float(os.environ.get("BRIDGE_POLL_SECONDS", "2"))
HEARTBEAT_SECONDS = float(os.environ.get("BRIDGE_HEARTBEAT_SECONDS", "30"))
# ingest throttles `last_seen` to one write a minute per agent, so anything
# fresher than two minutes is an agent that is delivering.
ALIVE_SECONDS = float(os.environ.get("BRIDGE_ALIVE_SECONDS", "120"))
SOURCE = "pipeline"
BATCH = 500
MAX_WINDOWS = 1440
# Latency rows read per poll. Each is one minute at most, so a poll sends at
# most four reports.
LATENCY_ROWS = 5000
# A sample lands in the minute its analysis ended, so a minute stops gaining
# rows within moments. An hour is long past that.
LATENCY_KEEP_HOURS = 1
MAX_MESSAGE = 8 * 1024
# Rows written before the pipeline assigned `anomaly_id` keep the id the bridge
# used to derive from the row id, so the ones already sent stay DUPLICATE.
NAMESPACE = uuid.UUID("5f0c2a8e-3b7d-4d1e-9a64-0b8f2c71e9d3")

log = logging.getLogger("bridge")

CURSORS_SCHEMA = """
CREATE TABLE IF NOT EXISTS bridge_cursors (name TEXT PRIMARY KEY, position BIGINT NOT NULL)
"""
READ_CURSOR = "SELECT position FROM bridge_cursors WHERE name = %s"
WRITE_CURSOR = """
INSERT INTO bridge_cursors (name, position) VALUES (%s, %s)
ON CONFLICT (name) DO UPDATE SET position = EXCLUDED.position
"""
SELECT_ANOMALIES = """
SELECT id, host, source, message, template_id, template, log_timestamp, score, threshold,
       model_key, model_revision, detected_at, anomaly_id, ingested_at, analyzed_at
FROM anomalies WHERE id > %s ORDER BY id LIMIT %s
"""
SELECT_LATENCY_NEW = "SELECT id, minute FROM latency_samples WHERE id > %s ORDER BY id LIMIT %s"
SELECT_LATENCY_MINUTES = """
SELECT minute, count, sum_ms, max_ms, bucket_counts FROM latency_samples WHERE minute = ANY(%s)
"""
# Only rows already sent. A minute this old gets no more samples, so its window
# never has to be rebuilt from them.
DELETE_LATENCY = """
DELETE FROM latency_samples WHERE id <= %s AND minute < now() - make_interval(hours => %s)
"""
SELECT_ALIVE = """
SELECT host FROM agents
WHERE revoked_at IS NULL AND last_seen > now() - make_interval(secs => %s)
GROUP BY host
"""


def read_cursor(connection, name: str) -> int:
    row = connection.execute(READ_CURSOR, (name,)).fetchone()
    return row[0] if row else 0


def write_cursor(connection, name: str, value: int) -> None:
    connection.execute(WRITE_CURSOR, (name, value))


def read_credentials() -> dict[str, tuple[str, str]]:
    """host -> (collector code, agent token), from the file tools/app_setup.py writes."""
    entries: dict[str, tuple[str, str]] = {}
    if not CREDENTIALS.is_file():
        return entries
    for line in CREDENTIALS.read_text().splitlines():
        # COLLECTOR_WEB1=collector-web1:token
        key, _, value = line.partition("=")
        if key.startswith("COLLECTOR_") and ":" in value:
            code, _, token = value.partition(":")
            entries[key.removeprefix("COLLECTOR_").lower()] = (code, token)
    return entries


def post(path: str, token: str, body: dict | None) -> tuple[int, dict | str]:
    """The status and the parsed body, or the raw text of an error response."""
    data = json.dumps(body or {}).encode()
    request = urllib.request.Request(
        f"{APP_URL}{path}", data, {"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        # The body is the only place the app says what it did not like.
        return error.code, error.read().decode(errors="replace")[:1000]


def iso(moment) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def to_report(row: tuple) -> dict:
    (row_id, host, source, message, template_id, template, log_timestamp, score, _threshold, _key, _revision,
     detected_at, anomaly_id, ingested_at, analyzed_at) = row
    return {
        "anomalyId": str(anomaly_id or uuid.uuid5(NAMESPACE, f"anomalies/{row_id}")),
        "host": host or "",
        "logSource": source,
        "message": message[:MAX_MESSAGE],
        "templateId": template_id,
        "template": template,
        "logTimestamp": log_timestamp,
        "score": min(max(float(score), 0.0), 1.0),
        # Rows from before the pipeline stamped them have one instant, for both.
        "ingestedAt": iso(ingested_at or detected_at),
        "analyzedAt": iso(analyzed_at or detected_at),
    }


def batches(rows: list[tuple]):
    """Consecutive runs of rows that share a model: a report carries one model."""
    run: list[tuple] = []
    for row in rows:
        if run and row[8:11] != run[0][8:11]:
            yield run
            run = []
        run.append(row)
    if run:
        yield run


def forward_anomalies(connection) -> None:
    cursor = read_cursor(connection, "anomalies")
    rows = connection.execute(SELECT_ANOMALIES, (cursor, BATCH)).fetchall()
    for run in batches(rows):
        threshold, model_key, revision = run[0][8], run[0][9], run[0][10]
        if revision == "local" and MODEL_REVISION:
            revision = MODEL_REVISION
        status, body = post("/api/v1/pipeline/anomalies", PIPELINE_TOKEN, {
            "source": SOURCE,
            "instanceId": INSTANCE_ID,
            "model": {"key": model_key, "revision": revision, "threshold": float(threshold)},
            "anomalies": [to_report(row) for row in run],
        })
        if status != 200:
            log.warning("app answered %s to %s anomalies; staying at row %s: %s", status, len(run), cursor, body)
            return
        # One line per distinct reason: a host with no node rejects every row it sent.
        # A rejected row is not sent again: the app would reject it the same way.
        rejected = Counter((r.get("reason"), r.get("detail")) for r in body.get("results", [])
                           if r.get("outcome") == "REJECTED")
        for (reason, detail), count in rejected.items():
            log.warning("rejected %s rows: %s %s", count, reason, detail)
        log.info("forwarded %s rows: %s accepted, %s duplicate, %s rejected; alerts created %s",
                 len(run), body.get("accepted"), body.get("duplicates"), body.get("rejected"),
                 body.get("alertsCreated"))
        cursor = run[-1][0]
        write_cursor(connection, "anomalies", cursor)


def forward_latency(connection) -> None:
    cursor = read_cursor(connection, "latency")
    new = connection.execute(SELECT_LATENCY_NEW, (cursor, LATENCY_ROWS)).fetchall()
    if not new:
        return
    # Every row of each minute that changed, not only the new ones: the app
    # replaces the window, so a partial sum would erase what it already had.
    minutes = sorted({minute for _, minute in new})
    windows: dict = {}
    for minute, count, sum_ms, max_ms, bucket_counts in connection.execute(
            SELECT_LATENCY_MINUTES, (minutes,)).fetchall():
        window = windows.setdefault(minute, {"count": 0, "sumMs": 0.0, "maxMs": 0.0, "bucketCounts": [0] * BUCKETS})
        window["count"] += count
        window["sumMs"] += sum_ms
        window["maxMs"] = max(window["maxMs"], max_ms)
        window["bucketCounts"] = [a + b for a, b in zip(window["bucketCounts"], bucket_counts, strict=True)]
    reports = [{"stage": "ANALYSIS", "windowStart": minute.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), **window}
               for minute, window in sorted(windows.items())]
    for start in range(0, len(reports), MAX_WINDOWS):
        chunk = reports[start:start + MAX_WINDOWS]
        status, body = post("/api/v1/pipeline/latency-windows", PIPELINE_TOKEN, {
            "source": SOURCE, "instanceId": INSTANCE_ID, "boundsMs": list(BOUNDS_MS), "windows": chunk})
        if status != 200:
            log.warning("app answered %s to %s latency windows; staying at row %s: %s",
                        status, len(chunk), cursor, body)
            return
    log.info("reported %s latency windows, %s samples in all", len(reports), sum(w["count"] for w in reports))
    cursor = new[-1][0]
    write_cursor(connection, "latency", cursor)
    connection.execute(DELETE_LATENCY, (cursor, LATENCY_KEEP_HOURS))


def send_heartbeats(connection) -> None:
    credentials = read_credentials()
    for (host,) in connection.execute(SELECT_ALIVE, (ALIVE_SECONDS,)).fetchall():
        if host not in credentials:
            continue
        code, token = credentials[host]
        status, _ = post(f"/api/v1/collectors/{code}/heartbeat", token, {})
        if status != 200:
            log.warning("heartbeat for %s answered %s", code, status)


def main() -> None:
    # Imported here, as ingest does, so tools/smoke_bridge.py runs without it.
    import psycopg

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("forwarding to %s as %s/%s", APP_URL, SOURCE, INSTANCE_ID)
    next_heartbeat = 0.0
    while True:
        try:
            with psycopg.connect(DSN, autocommit=True) as connection:
                connection.execute(CURSORS_SCHEMA)
                while True:
                    forward_anomalies(connection)
                    forward_latency(connection)
                    if time.monotonic() >= next_heartbeat:
                        send_heartbeats(connection)
                        next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
                    time.sleep(POLL_SECONDS)
        except (psycopg.Error, OSError, ValueError) as error:
            # Postgres restarting, the app unreachable or answering something
            # that is not JSON: back off and reconnect.
            log.warning("%s; retrying in 5 s", error)
            time.sleep(5)


if __name__ == "__main__":
    main()
