"""Bridge from the pipeline to the web app.

Two loops, one process:

- Anomalies. New rows of `anomalies` go to the app's
  `POST /api/v1/pipeline/anomalies`, at most 500 per batch, authenticated with
  the shared pipeline token. The app opens and groups the alerts.
- Heartbeats. Every agent whose `last_seen` is recent gets its collector's
  `POST /api/v1/collectors/{code}/heartbeat`, with that collector's own
  credential. That is what puts its node online in the app: "connected" means
  the agent is delivering logs, not that some process is alive.

Polls Postgres rather than hooking the collector: the hot path stays untouched,
and an app that is down or slow costs this process retries, never the pipeline
a stall. The cursor is the last forwarded row id, persisted after every batch
the app acknowledged, so a restart neither resends everything nor skips rows.
Re-sending is harmless anyway: `anomalyId` is derived from the row id, and the
app answers a known one with DUPLICATE.

Known ceiling: a collapsed row (same template, host and hour) is forwarded once,
when it first appears. The occurrences added to it later stay in the pipeline;
the app sees the first one, which is what opens the alert.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC
from pathlib import Path

import psycopg

DSN = os.environ["BRIDGE_DSN"]
APP_URL = os.environ["BRIDGE_APP_URL"].rstrip("/")
PIPELINE_TOKEN = os.environ["BRIDGE_PIPELINE_TOKEN"]
CREDENTIALS = Path(os.environ.get("BRIDGE_CREDENTIALS", "/creds/app-credentials.env"))
STATE = Path(os.environ.get("BRIDGE_STATE", "/state/cursor"))
MODEL_REVISION = os.environ.get("BRIDGE_MODEL_REVISION", "")
POLL_SECONDS = float(os.environ.get("BRIDGE_POLL_SECONDS", "2"))
HEARTBEAT_SECONDS = float(os.environ.get("BRIDGE_HEARTBEAT_SECONDS", "30"))
# ingest throttles `last_seen` to one write a minute per agent, so anything
# fresher than two minutes is an agent that is delivering.
ALIVE_SECONDS = float(os.environ.get("BRIDGE_ALIVE_SECONDS", "120"))
BATCH = 500
MAX_MESSAGE = 8 * 1024
# Fixed namespace: the same row always maps to the same anomalyId.
NAMESPACE = uuid.UUID("5f0c2a8e-3b7d-4d1e-9a64-0b8f2c71e9d3")

log = logging.getLogger("bridge")

SELECT_ANOMALIES = """
SELECT id, host, source, message, template_id, template, log_timestamp, score, threshold,
       model_key, model_revision, detected_at
FROM anomalies WHERE id > %s ORDER BY id LIMIT %s
"""
SELECT_ALIVE = """
SELECT host FROM agents
WHERE revoked_at IS NULL AND last_seen > now() - make_interval(secs => %s)
GROUP BY host
"""


def read_cursor() -> int:
    try:
        return int(STATE.read_text().strip() or 0)
    except FileNotFoundError:
        return 0


def write_cursor(value: int) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(str(value))
    tmp.replace(STATE)


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


def post(path: str, token: str, body: dict | None) -> tuple[int, dict | None]:
    data = json.dumps(body or {}).encode()
    request = urllib.request.Request(
        f"{APP_URL}{path}", data, {"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, None


def iso(moment) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def to_report(row: tuple) -> dict:
    row_id, host, source, message, template_id, template, log_timestamp, score, *_rest, detected_at = row
    return {
        "anomalyId": str(uuid.uuid5(NAMESPACE, f"anomalies/{row_id}")),
        "host": host or "",
        "logSource": source,
        "message": message[:MAX_MESSAGE],
        "templateId": template_id,
        "template": template,
        "logTimestamp": log_timestamp,
        "score": min(max(float(score), 0.0), 1.0),
        # The pipeline stamps one instant per scored row; it is both.
        "ingestedAt": iso(detected_at),
        "analyzedAt": iso(detected_at),
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
    cursor = read_cursor()
    rows = connection.execute(SELECT_ANOMALIES, (cursor, BATCH)).fetchall()
    for run in batches(rows):
        threshold, model_key, revision = run[0][8], run[0][9], run[0][10]
        if revision == "local" and MODEL_REVISION:
            revision = MODEL_REVISION
        status, body = post("/api/v1/pipeline/anomalies", PIPELINE_TOKEN, {
            "source": "pipeline",
            "instanceId": "demo",
            "model": {"key": model_key, "revision": revision, "threshold": float(threshold)},
            "anomalies": [to_report(row) for row in run],
        })
        if status >= 500 or status in (401, 403, 404, 0):
            # App down, misconfigured or not ready: keep the cursor, try again next poll.
            log.warning("app answered %s; will retry from row %s", status, cursor)
            return
        if status == 200 and body:
            for result in body.get("results", []):
                if result.get("outcome") == "REJECTED":
                    log.warning("rejected %s: %s %s", result.get("anomalyId"), result.get("reason"), result.get("detail"))
            log.info("forwarded %s rows: %s accepted, %s duplicate, %s rejected; alerts created %s",
                     len(run), body.get("accepted"), body.get("duplicates"), body.get("rejected"),
                     body.get("alertsCreated"))
        else:
            # A 400 rejects the whole batch and resending it would fail the same way.
            log.error("app rejected a batch of %s rows with %s; skipping it", len(run), status)
        cursor = run[-1][0]
        write_cursor(cursor)


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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("forwarding to %s from row %s", APP_URL, read_cursor())
    next_heartbeat = 0.0
    while True:
        try:
            with psycopg.connect(DSN, autocommit=True) as connection:
                while True:
                    forward_anomalies(connection)
                    if time.monotonic() >= next_heartbeat:
                        send_heartbeats(connection)
                        next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
                    time.sleep(POLL_SECONDS)
        except (psycopg.Error, OSError) as error:
            # Postgres restarting or the app unreachable: back off and reconnect.
            log.warning("%s; retrying in 5 s", error)
            time.sleep(5)


if __name__ == "__main__":
    main()
