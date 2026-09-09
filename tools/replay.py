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
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from parser import LogParser  # noqa: E402

DEFAULT_ENDPOINT = "http://localhost:8003"

# Measured throughput of the promoted model on CPU — ml/eval/results/modernbert-ft.json.
# The template report projects against this, so the projection moves when the
# model does instead of quoting a number someone typed once.
DETECTOR_RATE = 29.6


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(int(len(ordered) * fraction), len(ordered) - 1)], 2)


def template_report(total_records: int, unique_templates: int, raw_rate: float = DETECTOR_RATE) -> dict:
    """Project the score cache's effect from Drain3's template count.

    The cache in `log-collector` scores each template once and serves repeats
    for free, so the fraction of records that are *not* a template's first
    sighting is the hit rate. This is the ceiling, not the delivered number:
    it assumes an unbounded cache, no TTL expiry and no denylist bypass. The
    real hit rate is strictly lower, which is why the gate is set at 90% rather
    than at whatever this prints.
    """
    per_1000 = unique_templates / max(total_records, 1) * 1000
    hit_rate = 1.0 - (unique_templates / max(total_records, 1))
    miss_rate = max(1.0 - hit_rate, 1e-9)
    return {
        "records": total_records,
        "unique_templates": unique_templates,
        "templates_per_1000": round(per_1000, 1),
        "hit_rate_ceiling": round(hit_rate, 4),
        "effective_records_per_s": round(raw_rate / miss_rate, 1),
        "raw_records_per_s": raw_rate,
    }


def print_template_report(report: dict) -> None:
    print(f"records      : {report['records']:,}")
    print(f"templates    : {report['unique_templates']:,} unique "
          f"({report['templates_per_1000']} per 1,000 lines)")
    print(f"hit ceiling  : {report['hit_rate_ceiling']:.2%}  "
          f"(unbounded cache, no TTL, no denylist — the optimistic bound)")
    print(f"effective    : {report['effective_records_per_s']:,.0f} records/s "
          f"projected, from {report['raw_records_per_s']} records/s raw")


def run_ingest_load(args) -> int:
    """Offer a log file to `ingest-service` at a chosen rate.

    Deliberately does no parsing: the point is to behave like an agent, so the
    parsing, caching, shedding and backpressure all happen where they will in
    production instead of being short-circuited by this script.

    A 429 is not an error here. It is the pipeline asking the caller to hold,
    exactly as a real agent would be asked, and it is counted rather than
    retried so the report shows how much of the offered load was pushed back.
    """
    import httpx

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    sent = accepted = throttled = failed = 0
    batch: list[dict] = []
    started = time.perf_counter()

    def flush() -> None:
        nonlocal accepted, throttled, failed
        if not batch:
            return
        try:
            response = client.post(f"{args.ingest}/ingest", json={"records": batch}, headers=headers, timeout=60.0)
        except Exception:  # noqa: BLE001
            failed += len(batch)
            batch.clear()
            return
        if response.status_code == 429:
            throttled += len(batch)
        elif response.status_code < 300:
            accepted += len(batch)
        else:
            failed += len(batch)
            if failed <= len(batch):
                print(f"ingest error {response.status_code}: {response.text[:200]}", file=sys.stderr)
        batch.clear()

    with httpx.Client() as client:
        with args.logfile.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                batch.append({"schema_version": "1.0", "host": args.host or "loadtest",
                              "source": "loadtest", "message": line[:16000], "level": None})
                sent += 1
                if len(batch) >= args.batch_size:
                    flush()
                    if args.rate:
                        # Pace against the offered rate rather than sleeping a
                        # fixed amount, so the report's "offered" figure is real.
                        target = started + sent / args.rate
                        drift = target - time.perf_counter()
                        if drift > 0:
                            time.sleep(drift)
                if args.limit and sent >= args.limit:
                    break
            flush()

    elapsed = time.perf_counter() - started
    print(f"offered  : {sent:,} lines in {elapsed:.1f}s ({sent / max(elapsed, 1e-9):.0f} lines/s)")
    print(f"accepted : {accepted:,}")
    print(f"throttled: {throttled:,} (429 — pipeline asked the agent to hold)")
    if failed:
        print(f"failed   : {failed:,}")
    return 1 if failed else 0


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
    parser.add_argument("--dsn", default="postgresql://anomaly:anomaly@localhost:5432/anomaly_watch")
    parser.add_argument("--show", type=int, default=5, help="print the N highest-scoring lines")
    parser.add_argument("--assert-anomalies", type=int, default=None,
                        help="CI mode: exit non-zero unless at least N anomalies are flagged")
    parser.add_argument("--ingest", default=None, metavar="URL",
                        help="Load-generator mode: post raw lines to ingest-service instead of "
                             "scoring directly. Drives the full agent path, so backpressure and "
                             "the drop policy are exercised rather than bypassed")
    parser.add_argument("--token", default=None, help="agent token for --ingest mode")
    parser.add_argument("--rate", type=float, default=None,
                        help="lines/s to offer. Omit for as fast as possible")
    parser.add_argument("--template-stats", action="store_true",
                        help="Parse only: report template repetition and projected cache hit rate. "
                             "No scoring, so it needs neither the model nor the service")
    args = parser.parse_args()

    if not args.logfile.exists():
        print(f"No such file: {args.logfile}", file=sys.stderr)
        return 1

    if args.ingest:
        return run_ingest_load(args)

    if args.template_stats:
        # Deliberately its own path: the measurement that gates the cache design
        # must not require the thing it is measuring the need for.
        stats_parser = LogParser()
        parsed = 0
        with args.logfile.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if stats_parser.parse(line) is None:
                    continue
                parsed += 1
                if args.limit and parsed >= args.limit:
                    break
        print(f"source       : {args.logfile}")
        print_template_report(template_report(parsed, stats_parser.template_count))
        return 0

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
    print()
    print_template_report(template_report(len(scored_rows), log_parser.template_count))

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
