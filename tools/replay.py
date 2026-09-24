"""Replay a log file through Drain3 and detector-service.

This is the demo-phase transport. It exists so the model layer can be exercised
end to end without a broker: Kafka is the target topology, but wiring it before
the detector's real batch size and latency are known would mean designing the
stream against guesses.

Also doubles as the CI smoke test (--assert-anomalies), which runs offline
against the bundled sample.

With --report-metrics it also measures how long each line takes from being read
to being scored, and reports that latency to AnomalyWatch as per-minute
histograms. Reporting never affects detection or the exit code.

Usage:
    python tools/replay.py data/samples/HDFS_2k.log
    python tools/replay.py data/samples/HDFS_2k.log --store-postgres
    python tools/replay.py data/samples/HDFS_2k.log --assert-anomalies 1
    PIPELINE_SERVICE_TOKEN=... python tools/replay.py data/samples/HDFS_2k.log \\
        --report-metrics --aw-endpoint http://localhost:8080
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from latency import DEFAULT_BUFFER_SIZE, Reporter, Stamp, WindowAggregator, utc_now  # noqa: E402
from parser import LogParser  # noqa: E402

DEFAULT_ENDPOINT = "http://localhost:8003"
TOKEN_VARIABLE = "PIPELINE_SERVICE_TOKEN"


def reporting_config_errors(report_metrics: bool, aw_endpoint: str | None, token: str | None) -> list[str]:
    """What is missing to report latency. Empty when reporting is off or fully configured."""
    if not report_metrics:
        return []
    errors = []
    if not aw_endpoint:
        errors.append("--aw-endpoint is required with --report-metrics")
    if not token:
        errors.append(f"{TOKEN_VARIABLE} must be set in the environment with --report-metrics")
    return errors


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(int(len(ordered) * fraction), len(ordered) - 1)], 2)


def post_batch(client, endpoint: str, records: list[dict]) -> dict:
    response = client.post(f"{endpoint}/score", json={"records": records}, timeout=120.0)
    if response.status_code == 503:
        raise RuntimeError(
            f"detector-service is not ready: {response.text}. "
            f"Start it with: uv run uvicorn services.detector.app:app --port 8003"
        )
    response.raise_for_status()
    return response.json()


def store_postgres(rows: list[dict], dsn: str, model_key: str, model_revision: str, threshold: float) -> int:
    """Persist flagged anomalies. Model id and revision go in the row.

    Without them, an old anomaly cannot be interpreted after a model swap — the
    score would have no meaning attached to it.
    """
    import psycopg

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS anomalies (
                    id            BIGSERIAL PRIMARY KEY,
                    detected_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                    host          TEXT,
                    log_timestamp TEXT,
                    template_id   TEXT,
                    template      TEXT,
                    message       TEXT        NOT NULL,
                    score         REAL        NOT NULL,
                    threshold     REAL        NOT NULL,
                    model_key     TEXT        NOT NULL,
                    model_revision TEXT       NOT NULL,
                    ingested_at   TIMESTAMPTZ,
                    analyzed_at   TIMESTAMPTZ
                )
                """
            )
            # CREATE TABLE IF NOT EXISTS does not touch a table created before these
            # columns existed; older rows keep them NULL.
            cursor.execute("ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ")
            cursor.execute("ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS analyzed_at TIMESTAMPTZ")
            cursor.executemany(
                """
                INSERT INTO anomalies
                    (host, log_timestamp, template_id, template, message, score, threshold,
                     model_key, model_revision, ingested_at, analyzed_at)
                VALUES (%(host)s, %(timestamp)s, %(template_id)s, %(template)s, %(message)s,
                        %(score)s, %(threshold)s, %(model_key)s, %(model_revision)s,
                        %(ingested_at)s, %(analyzed_at)s)
                """,
                [
                    {
                        "host": row.get("host"),
                        "timestamp": row.get("timestamp"),
                        "template_id": row.get("template_id"),
                        "template": row.get("template"),
                        "message": row["message"],
                        "score": row["score"],
                        "threshold": threshold,
                        "model_key": model_key,
                        "model_revision": model_revision,
                        "ingested_at": row.get("ingested_at"),
                        "analyzed_at": row.get("analyzed_at"),
                    }
                    for row in rows
                ],
            )
        connection.commit()
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("logfile", type=Path)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--host", default=None, help="source host label for the records")
    parser.add_argument("--out", type=Path, default=None, help="write scored records as JSONL")
    parser.add_argument("--store-postgres", action="store_true")
    parser.add_argument("--dsn", default="postgresql://anomaly:anomaly@localhost:5432/anomaly_watch")
    parser.add_argument("--show", type=int, default=5, help="print the N highest-scoring lines")
    parser.add_argument("--assert-anomalies", type=int, default=None,
                        help="CI mode: exit non-zero unless at least N anomalies are flagged")
    parser.add_argument("--report-metrics", action="store_true",
                        help=f"report analysis latency to AnomalyWatch (token from {TOKEN_VARIABLE})")
    parser.add_argument("--aw-endpoint", default=None, help="AnomalyWatch API base URL, e.g. http://localhost:8080")
    parser.add_argument("--metrics-buffer", type=int, default=DEFAULT_BUFFER_SIZE,
                        help="max unconfirmed latency windows kept for retry (oldest dropped first)")
    args = parser.parse_args()

    if not args.logfile.exists():
        print(f"No such file: {args.logfile}", file=sys.stderr)
        return 1

    token = os.environ.get(TOKEN_VARIABLE)
    config_errors = reporting_config_errors(args.report_metrics, args.aw_endpoint, token)
    if config_errors:
        for error in config_errors:
            print(f"Cannot report metrics: {error}", file=sys.stderr)
        return 2

    import httpx

    log_parser = LogParser()
    scored_rows: list[dict] = []
    batch_latencies: list[float] = []
    threshold = 0.5
    model_key = model_revision = "unknown"
    aggregator = WindowAggregator()

    started = time.perf_counter()
    with httpx.Client() as client:
        try:
            info = client.get(f"{args.endpoint}/model", timeout=30.0).json()
            print(f"detector : {info['repo_id']} @ {info['revision'][:12]} (threshold {info['threshold']})")
        except Exception as exc:  # noqa: BLE001
            print(f"Cannot reach detector-service at {args.endpoint}: {exc}", file=sys.stderr)
            print("Start it with: uv run uvicorn services.detector.app:app --port 8003", file=sys.stderr)
            return 1

        reporter = None
        if args.report_metrics:
            reporter = Reporter(
                client, args.aw_endpoint, token, source="replay",
                instance_id=f"replay-{uuid.uuid4().hex[:8]}", buffer_size=args.metrics_buffer,
                log=lambda message: print(message, file=sys.stderr),
            )

        # batch holds exactly what /score declares; timing stays on this side of the contract.
        batch: list[dict] = []
        stamps: list[Stamp] = []
        total_parsed = 0

        def flush() -> None:
            nonlocal threshold, model_key, model_revision
            if not batch:
                return
            batch_started = time.perf_counter()
            payload = post_batch(client, args.endpoint, batch)
            analyzed = Stamp.now()
            batch_latencies.append((time.perf_counter() - batch_started) * 1000)
            threshold = payload["threshold"]
            model_key = payload["model_key"]
            model_revision = payload["model_revision"]
            for record, stamp, result in zip(batch, stamps, payload["results"], strict=True):
                aggregator.add(stamp.elapsed_ms_until(analyzed), analyzed.wall)
                scored_rows.append({
                    **record,
                    "score": result["score"],
                    "is_anomaly": result["is_anomaly"],
                    "ingested_at": stamp.wall,
                    "analyzed_at": analyzed.wall,
                })
            batch.clear()
            stamps.clear()

            # Between /score calls, never during one: reporting cannot delay scoring.
            if reporter is not None:
                closed = aggregator.close_due(utc_now())
                if closed:
                    reporter.enqueue(closed)
                    reporter.flush()

        with args.logfile.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                received = Stamp.now()
                record = log_parser.parse(line, host=args.host)
                if record is None:
                    continue
                total_parsed += 1
                batch.append(record.to_payload())
                stamps.append(received)
                if len(batch) >= args.batch_size:
                    flush()
                if args.limit and total_parsed >= args.limit:
                    break
            flush()

        if reporter is not None:
            reporter.enqueue(aggregator.close_all())
            reporter.flush()

    elapsed = time.perf_counter() - started
    anomalies = [row for row in scored_rows if row["is_anomaly"]]

    print(f"\nparsed   : {len(scored_rows):,} records into {log_parser.template_count} Drain3 templates")
    print(f"flagged  : {len(anomalies):,} anomalies "
          f"({len(anomalies) / max(len(scored_rows), 1):.2%}) at threshold {threshold}")
    print(f"elapsed  : {elapsed:.1f}s  ({len(scored_rows) / max(elapsed, 1e-9):.0f} records/s end to end)")
    print(f"batch p50/p95: {percentile(batch_latencies, 0.5)}ms / {percentile(batch_latencies, 0.95)}ms "
          f"(batch size {args.batch_size})")
    if reporter is not None:
        print(f"latency  : {reporter.summary()}")

    if args.show and anomalies:
        print(f"\ntop {args.show} by score:")
        for row in sorted(anomalies, key=lambda r: r["score"], reverse=True)[: args.show]:
            print(f"  {row['score']:.4f}  {row['message'][:96]}")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8") as handle:
            for row in scored_rows:
                handle.write(json.dumps(row, ensure_ascii=False, default=_json_default) + "\n")
        print(f"\nwrote {args.out}")

    if args.store_postgres:
        stored = store_postgres(anomalies, args.dsn, model_key, model_revision, threshold)
        print(f"stored {stored:,} anomalies in Postgres")

    if args.assert_anomalies is not None:
        if len(anomalies) < args.assert_anomalies:
            print(f"\nFAIL: expected >= {args.assert_anomalies} anomalies, got {len(anomalies)}",
                  file=sys.stderr)
            return 1
        print(f"\nPASS: {len(anomalies)} >= {args.assert_anomalies} anomalies flagged")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
