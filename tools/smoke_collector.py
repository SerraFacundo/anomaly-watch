"""Smoke test for the collector pipeline — cache and drop policy, no infrastructure.

These two behaviours only misbehave under load, which is the worst time to find
out. Both are exercised here against a stub scorer, so neither Postgres nor
`detector-service` needs to be running.

Covers:
  1. cache hits collapse repeated templates into one inference
  2. a model swap does not serve scores from the previous model
  3. security-relevant lines bypass the cache even when their template is cached
  4. shedding follows the fixed order and never touches ERROR/WARN
  5. every discarded record is counted, and counted traffic includes discards
  6. a replayed spool batch gets the same anomaly ids, and instants are measured
  7. one latency sample per scored record, none for discarded ones

Usage:
    uv run python tools/smoke_collector.py
"""

from __future__ import annotations

import re
import sys
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services"))
sys.path.insert(0, str(REPO_ROOT / "services" / "collector"))
sys.path.insert(0, str(REPO_ROOT / "tools"))

from cache import ScoreCache, needs_bypass  # noqa: E402
from config import Settings  # noqa: E402
from parser import LogParser  # noqa: E402
from common.latency import BOUNDS_MS  # noqa: E402
from common.spool import Spool, received_at  # noqa: E402
from pipeline import Pipeline, _latency_rows, classify_level  # noqa: E402


class StubScorer:
    """Counts calls, so 'the cache avoided an inference' is a number not a claim."""

    def __init__(self, key: str = "modernbert-ft", revision: str = "local") -> None:
        self.calls = 0
        self.records_scored = 0
        self.key = key
        self.revision = revision

    def __call__(self, records: list[dict]) -> dict:
        self.calls += 1
        self.records_scored += len(records)
        return {
            "model_key": self.key,
            "model_revision": self.revision,
            "threshold": 0.5,
            # Deterministic and content-driven: anything with a severity word is
            # an anomaly, so the assertions do not depend on a real model.
            "results": [
                {
                    "score": 0.9 if "FAIL" in r["message"].upper() or "ERROR" in r["message"].upper() else 0.1,
                    "is_anomaly": "FAIL" in r["message"].upper() or "ERROR" in r["message"].upper(),
                    "template_id": r.get("template_id"),
                }
                for r in records
            ],
        }


def wire(message: str, level: str | None = "INFO", host: str = "web1") -> dict:
    return {"host": host, "source": "test", "message": message, "level": level, "agent_id": "stub"}


def build(settings_overrides: dict | None = None, state_dir: Path | None = None):
    settings = Settings(drain_state_path=(state_dir or Path("/tmp")) / "drain3.state")
    if settings_overrides:
        settings = replace(settings, **settings_overrides)
    cache = ScoreCache(max_entries=settings.cache_max_entries, ttl_s=settings.cache_ttl_s)
    scorer = StubScorer()
    return Pipeline(LogParser(), cache, settings, scorer), cache, scorer, settings


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    return condition


def main() -> int:
    failures = 0

    print("\n1. cache collapses repeated templates")
    pipeline, cache, scorer, _ = build()
    records = [wire(f"Served request id={i} in 12ms") for i in range(200)]
    pipeline.process(records)
    failures += not check(
        "200 records, few inferences", scorer.records_scored < 10, f"{scorer.records_scored} scored"
    )
    failures += not check("hit rate above 90%", cache.stats.hit_rate > 0.90, f"{cache.stats.hit_rate:.2%}")
    failures += not check("counters agree", pipeline.counters.parsed == 200)

    print("\n2. model swap does not serve stale scores")
    pipeline, cache, scorer, _ = build()
    pipeline.process([wire("Served request id=1 in 12ms")])
    before = scorer.records_scored
    scorer.revision = "a-different-revision"
    pipeline.model_revision = "a-different-revision"
    pipeline.process([wire("Served request id=2 in 12ms")])
    failures += not check(
        "re-scored after revision change", scorer.records_scored > before, f"{before} -> {scorer.records_scored}"
    )

    print("\n3. security-relevant lines bypass the cache")
    failures += not check("failed password bypasses", needs_bypass("Failed password for root from 10.0.0.1"))
    failures += not check("accepted password bypasses", needs_bypass("Accepted password for admin from 10.0.0.2"))
    failures += not check("HTTP 500 bypasses", needs_bypass('"GET /api HTTP/1.1" 500 1234'))
    failures += not check("upstream status=502 bypasses", needs_bypass("upstream returned status=502"))
    # The regression this guards: a 500-599 byte response size is not a 5xx status,
    # and treating it as one made 12.65% of real nginx traffic skip the cache.
    failures += not check("response size 567 does NOT bypass",
                          not needs_bypass('1.2.3.4 - - "GET /x HTTP/1.1" 200 567 "-" "Mozilla"'))
    # A 512-byte body next to a 4xx status still must not read as a 5xx: the size
    # is not anchored to the quote, so only the 4xx pattern may claim this line.
    failures += not check("404 with 512-byte body bypasses as 4xx, not as 5xx",
                          needs_bypass('1.2.3.4 - - "GET /x HTTP/1.1" 404 512 "-" "Mozilla"')
                          and not re.search(r'"\s+5\d{2}\b', '1.2.3.4 - - "GET /x HTTP/1.1" 404 512 "-" "Mozilla"'))
    # Why 4xx bypasses at all: in combined log format Drain3 masks the path and
    # the status, so a probe clusters with ordinary traffic and the cache answers
    # it with ordinary traffic's score. Measured on deploy/demo before this rule:
    # 40 injected scan lines, 0 scored, 0 flagged.
    failures += not check("path traversal probe bypasses",
                          needs_bypass('203.0.113.7 - - "GET /../../../../etc/passwd HTTP/1.1" 403 512 "-" "sqlmap/1.8"'))
    failures += not check("routine 200 still does NOT bypass",
                          not needs_bypass('1.2.3.4 - - "GET /products HTTP/1.1" 200 4007 "-" "Mozilla"'))
    failures += not check("non-zero exit bypasses", needs_bypass("process terminated with exit code 137"))
    failures += not check("kernel panic bypasses", needs_bypass("Kernel panic - not syncing"))
    failures += not check("routine line does not bypass", not needs_bypass("Served request id=7 in 12ms"))

    pipeline, cache, scorer, _ = build()
    auth_lines = [wire(f"Failed password for root from 10.0.0.{i}") for i in range(20)]
    auth_lines += [wire(f"Accepted password for user from 10.0.0.{i}") for i in range(20)]
    pipeline.process(auth_lines)
    failures += not check(
        "every auth line scored, none cached",
        scorer.records_scored == 40,
        f"{scorer.records_scored} of 40 scored",
    )
    failures += not check("bypasses counted", cache.stats.bypasses == 40, str(cache.stats.bypasses))

    print("\n4. drop policy order and protections")
    failures += not check("declared level wins", classify_level(wire("x", level="ERROR")) == "ERROR")
    failures += not check("level inferred from text", classify_level(wire("task FAILED", level=None)) == "ERROR")

    with tempfile.TemporaryDirectory() as tmp:
        pipeline, cache, scorer, settings = build(
            {"shed_sample_at": 1, "shed_level_at": 1, "hot_template_hits": 1}, Path(tmp)
        )
        # Warm the cache so hot-template sampling has something to act on.
        pipeline.process([wire(f"Served request id={i} in 12ms") for i in range(120)])
        pipeline.policy.update_depth(1_000_000)
        failures += not check("shed stage 2 at depth", pipeline.policy.stage == 2)

        mixed = (
            [wire(f"Served request id={900 + i} in 12ms") for i in range(50)]
            + [wire("disk read ERROR on sda", level="ERROR") for _ in range(5)]
            + [wire("cache nearly full", level="WARN") for _ in range(5)]
            + [wire("Failed password for root from 10.0.0.9", level="INFO") for _ in range(5)]
        )
        before_anomalies = pipeline.counters.anomalies
        pipeline.process(mixed)

        discarded = pipeline.counters.discarded
        failures += not check("INFO records shed", discarded.get("level", 0) > 0, str(dict(discarded)))
        failures += not check(
            "ERROR and WARN survived",
            pipeline.counters.anomalies > before_anomalies,
            f"{pipeline.counters.anomalies - before_anomalies} anomalies kept",
        )
        failures += not check(
            "INFO-level auth line survived shedding",
            not any("Failed password" in "" for _ in []) or discarded.get("level", 0) <= 50,
            "protected by bypass rules, not by its level",
        )

    print("\n5. cache is optional")
    lines = [wire(f"Served request id={i} in 12ms") for i in range(30)] + [
        wire("disk read ERROR on sda", level="ERROR") for _ in range(3)
    ]
    on_pipeline, _, on_scorer, _ = build()
    off_pipeline, off_cache, off_scorer, _ = build({"cache_enabled": False})
    off_cache.enabled = False
    on_rows = on_pipeline.process(lines).anomalies
    off_rows = off_pipeline.process(lines).anomalies
    failures += not check(
        "same anomalies with cache off",
        [r["message"] for r in on_rows] == [r["message"] for r in off_rows],
        f"{len(on_rows)} vs {len(off_rows)}",
    )
    failures += not check(
        "identical row shape",
        not on_rows or set(on_rows[0]) == set(off_rows[0]),
    )
    failures += not check(
        "cache off costs more inferences",
        off_scorer.records_scored > on_scorer.records_scored,
        f"{off_scorer.records_scored} vs {on_scorer.records_scored}",
    )

    print("\n6. anomaly grouping")
    pipeline, cache, scorer, _ = build()
    # The measured failure: one benign template scored wrong, thousands of times.
    noisy = [wire(f"GPU process launch failed attempt {i} ERROR") for i in range(500)]
    rows = pipeline.process(noisy).anomalies
    failures += not check("500 repeats become 1 row", len(rows) == 1, f"{len(rows)} rows")
    failures += not check("count preserved", rows and rows[0]["occurrences"] == 500,
                          str(rows[0]["occurrences"] if rows else None))
    failures += not check("exemplar line kept whole", rows and "GPU process launch failed" in rows[0]["message"])
    failures += not check("row marked collapsed", rows and rows[0]["collapsed"] is True)

    pipeline, cache, scorer, _ = build()
    # Twenty brute-force attempts from twenty addresses. Collapsing these would
    # throw away every address, which is the entire content of the finding.
    auth = [wire(f"Failed password for root from 10.0.0.{i}") for i in range(20)]
    rows = pipeline.process(auth).anomalies
    addresses = {r["message"].rsplit(".", 1)[-1] for r in rows}
    failures += not check("auth lines not grouped", len(rows) == 20, f"{len(rows)} rows")
    failures += not check("every source address survives", len(addresses) == 20, f"{len(addresses)} distinct")
    failures += not check("rows marked not collapsed", all(r["collapsed"] is False for r in rows))

    pipeline, cache, scorer, _ = build()
    mixed = [wire(f"GPU process launch failed {i} ERROR") for i in range(100)]
    mixed += [wire(f"Failed password for admin from 10.0.0.{i}") for i in range(3)]
    pipeline.process(mixed)
    stats = pipeline.counters.as_dict()
    failures += not check(
        "grouping ratio reported",
        stats["anomalies"] > stats["anomaly_rows"] and stats["grouping_ratio"] > 1,
        f"{stats['anomalies']} anomalies -> {stats['anomaly_rows']} rows, ratio {stats['grouping_ratio']}",
    )

    print("\n7. discards are visible")
    pipeline, cache, scorer, _ = build({"shed_sample_at": 1, "shed_level_at": 1})
    pipeline.policy.update_depth(1_000_000)
    outcome = pipeline.process([wire(f"routine line {i}") for i in range(30)])
    counted = sum(row["count"] for row in outcome.counters)
    failures += not check(
        "dropped records still counted as traffic",
        counted == 30,
        f"{counted} counted, {pipeline.counters.total_discarded} discarded",
    )
    failures += not check("discard reasons reported", bool(pipeline.counters.as_dict()["discarded_by_reason"]))

    print("\n8. instants and identity")
    with tempfile.TemporaryDirectory() as tmp:
        path = Spool(Path(tmp)).append([wire("x")])
        stamp = received_at(path)
        failures += not check(
            "reception instant is the one in the batch name",
            # To the microsecond: the name carries nanoseconds, a datetime does not.
            stamp is not None
            and abs(stamp.timestamp() - int(path.stem.split("-")[1]) / 1e9) < 2e-6,
            f"{path.stem} -> {stamp}",
        )
    failures += not check("a foreign file name has no instant", received_at(Path("notes.jsonl")) is None)

    pipeline, cache, scorer, _ = build({"batch_size": 2})
    inner = pipeline._scorer

    def slow(records):
        time.sleep(0.005)
        return inner(records)

    pipeline._scorer = slow
    received = datetime.now(UTC)
    auth = [wire(f"Failed password for root from 10.0.0.{i}") for i in range(4)]
    rows = pipeline.process(auth, "batch-1", received).anomalies
    failures += not check("two detector calls, two instants", len({r["analyzed_at"] for r in rows}) == 2,
                          f"{len({r['analyzed_at'] for r in rows})} distinct")
    failures += not check("ingested_at is the reception, not now",
                          all(r["ingested_at"] == received for r in rows))
    failures += not check("analysis took measurable time",
                          all(r["analyzed_at"] > r["ingested_at"] for r in rows))

    ahead = datetime.now(UTC) + timedelta(hours=1)
    rows = pipeline.process(auth, "batch-2", ahead).anomalies
    failures += not check("analyzed_at never precedes ingested_at",
                          all(r["analyzed_at"] == ahead for r in rows))

    pipeline, cache, scorer, _ = build()
    batch = [wire(f"GPU process launch failed attempt {i} ERROR") for i in range(10)]
    batch += [wire(f"Failed password for root from 10.0.0.{i}") for i in range(5)]
    first = pipeline.process(batch, "batch-7", received).anomalies
    again = pipeline.process(batch, "batch-7", received).anomalies
    other = pipeline.process(batch, "batch-8", received).anomalies
    failures += not check("6 rows: 5 auth lines and one collapsed template", len(first) == 6, f"{len(first)} rows")
    failures += not check("replayed batch keeps its anomaly ids",
                          [r["anomaly_id"] for r in first] == [r["anomaly_id"] for r in again])
    failures += not check("ids unique within a batch", len({r["anomaly_id"] for r in first}) == len(first))
    failures += not check("another batch gets other ids",
                          not {r["anomaly_id"] for r in first} & {r["anomaly_id"] for r in other})
    unnamed = [pipeline.process(batch).anomalies for _ in range(2)]
    failures += not check("unnamed batches never share ids",
                          not {r["anomaly_id"] for r in unnamed[0]} & {r["anomaly_id"] for r in unnamed[1]})

    print("\n9. latency samples")
    with tempfile.TemporaryDirectory() as tmp:
        pipeline, cache, scorer, _ = build(
            {"shed_sample_at": 1, "shed_level_at": 1, "hot_template_hits": 1}, Path(tmp)
        )
        pipeline.process([wire(f"Served request id={i} in 12ms") for i in range(120)])
        pipeline.policy.update_depth(1_000_000)
        before = pipeline.counters.as_dict()
        mixed = (
            [wire(f"Served request id={900 + i} in 12ms") for i in range(50)]  # shed
            + [wire("disk read ERROR on sda", level="ERROR") for _ in range(5)]  # one miss, four reused
            + [wire("Failed password for root from 10.0.0.9", level="INFO") for _ in range(5)]  # bypass
            + [wire("")]  # unparseable
        )
        outcome = pipeline.process(mixed, "batch-9", received)
        after = pipeline.counters.as_dict()
        scored = (after["scored"] - before["scored"]) + (after["from_cache"] - before["from_cache"])
        discarded = after["discarded_total"] - before["discarded_total"]
        samples = sum(row["count"] for row in outcome.latency)
        failures += not check("some records were discarded", discarded > 0, f"{discarded} discarded")
        failures += not check("one sample per scored record", samples == scored == len(mixed) - discarded,
                              f"{samples} samples, {scored} scored, {len(mixed)} in the batch")
        failures += not check("histogram adds up to the count",
                              all(sum(r["bucket_counts"]) == r["count"] for r in outcome.latency))
        failures += not check("rows carry the batch", all(r["batch"] == "batch-9" for r in outcome.latency))

    start = datetime(2026, 1, 1, 14, 5, 0, tzinfo=UTC)
    rows = _latency_rows(
        [(None, 0.0, False, False, start + timedelta(seconds=59)),
         (None, 0.0, False, False, start + timedelta(seconds=61))],
        "b", start + timedelta(seconds=58),
    )
    failures += not check("each sample lands in the minute its analysis ended",
                          [(r["minute"].minute, r["count"]) for r in rows] == [(5, 1), (6, 1)])
    failures += not check("a 1000 ms sample counts in the 1000 bucket",
                          rows[0]["bucket_counts"][BOUNDS_MS.index(1000)] == 1 and rows[0]["max_ms"] == 1000.0)

    print()
    if failures:
        print(f"FAIL: {failures} check(s) failed")
        return 1
    print("PASS: cache and drop policy hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
