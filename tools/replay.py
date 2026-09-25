"""Replay a log file through Drain3 and detector-service.

This is the demo-phase transport. It exists so the model layer can be exercised
end to end without a broker: Kafka is the target topology, but wiring it before
the detector's real batch size and latency are known would mean designing the
stream against guesses.

Also doubles as the CI smoke test (--assert-anomalies), which runs offline
against the bundled sample.

Usage:
    python tools/replay.py data/samples/HDFS_2k.log
    python tools/replay.py data/samples/HDFS_2k.log --store-postgres
    python tools/replay.py data/samples/HDFS_2k.log --assert-anomalies 1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from parser import LogParser  # noqa: E402

DEFAULT_ENDPOINT = "http://localhost:8003"
# Never the product stack's database: that one lives on 5432, this repo's
# compose Postgres is published on 5433. ANOMALY_WATCH_ML_DSN (set inside the
# stack's own `replay` container) takes precedence; --dsn overrides both.
DEFAULT_DSN = "postgresql://anomaly:anomaly@localhost:5433/anomaly_watch"


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
                    model_revision TEXT       NOT NULL
                )
                """
            )
            cursor.executemany(
                """
                INSERT INTO anomalies
                    (host, log_timestamp, template_id, template, message, score, threshold,
                     model_key, model_revision)
                VALUES (%(host)s, %(timestamp)s, %(template_id)s, %(template)s, %(message)s,
                        %(score)s, %(threshold)s, %(model_key)s, %(model_revision)s)
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
    parser.add_argument("--dsn", default=os.environ.get("ANOMALY_WATCH_ML_DSN", DEFAULT_DSN))
    parser.add_argument("--show", type=int, default=5, help="print the N highest-scoring lines")
    parser.add_argument("--assert-anomalies", type=int, default=None,
                        help="CI mode: exit non-zero unless at least N anomalies are flagged")
    args = parser.parse_args()

    if not args.logfile.exists():
        print(f"No such file: {args.logfile}", file=sys.stderr)
        return 1

    import httpx

    log_parser = LogParser()
    scored_rows: list[dict] = []
    batch_latencies: list[float] = []
    threshold = 0.5
    model_key = model_revision = "unknown"

    started = time.perf_counter()
    with httpx.Client() as client:
        try:
            info = client.get(f"{args.endpoint}/model", timeout=30.0).json()
            print(f"detector : {info['repo_id']} @ {info['revision'][:12]} (threshold {info['threshold']})")
        except Exception as exc:  # noqa: BLE001
            print(f"Cannot reach detector-service at {args.endpoint}: {exc}", file=sys.stderr)
            print("Start it with: uv run uvicorn services.detector.app:app --port 8003", file=sys.stderr)
            return 1

        batch: list[dict] = []
        parsed_batch: list[dict] = []
        total_parsed = 0

        def flush() -> None:
            nonlocal threshold, model_key, model_revision
            if not batch:
                return
            batch_started = time.perf_counter()
            payload = post_batch(client, args.endpoint, batch)
            batch_latencies.append((time.perf_counter() - batch_started) * 1000)
            threshold = payload["threshold"]
            model_key = payload["model_key"]
            model_revision = payload["model_revision"]
            for record, result in zip(parsed_batch, payload["results"], strict=True):
                scored_rows.append({**record, "score": result["score"], "is_anomaly": result["is_anomaly"]})
            batch.clear()
            parsed_batch.clear()

        with args.logfile.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                record = log_parser.parse(line, host=args.host)
                if record is None:
                    continue
                total_parsed += 1
                parsed_batch.append(record.to_payload())
                batch.append(record.to_payload())
                if len(batch) >= args.batch_size:
                    flush()
                if args.limit and total_parsed >= args.limit:
                    break
            flush()

    elapsed = time.perf_counter() - started
    anomalies = [row for row in scored_rows if row["is_anomaly"]]

    print(f"\nparsed   : {len(scored_rows):,} records into {log_parser.template_count} Drain3 templates")
    print(f"flagged  : {len(anomalies):,} anomalies "
          f"({len(anomalies) / max(len(scored_rows), 1):.2%}) at threshold {threshold}")
    print(f"elapsed  : {elapsed:.1f}s  ({len(scored_rows) / max(elapsed, 1e-9):.0f} records/s end to end)")
    print(f"batch p50/p95: {percentile(batch_latencies, 0.5)}ms / {percentile(batch_latencies, 0.95)}ms "
          f"(batch size {args.batch_size})")

    if args.show and anomalies:
        print(f"\ntop {args.show} by score:")
        for row in sorted(anomalies, key=lambda r: r["score"], reverse=True)[: args.show]:
            print(f"  {row['score']:.4f}  {row['message'][:96]}")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8") as handle:
            for row in scored_rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
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
