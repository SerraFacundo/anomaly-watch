"""Smoke test for the Redis log queue, `ingest-service`, and `log-collector` batching.

Exercises the full contract end to end without requiring Docker (via a faithful
thread-safe in-memory Redis client) or against a real Redis server when
`--redis-url` is supplied:

  1. `POST /ingest` authenticates, stamps `agent_id`, and enqueues into Redis (`202`)
  2. Greedy non-waiting drain: 5 queued records with ceiling 128 return immediately
     without waiting to fill the batch; `BLMOVE` wakes reactively on arrival
  3. Burst ceiling: 300 records drain in chunks of 128 + 128 + 44, and `Pipeline`
     scores misses in sub-batches capped at 32 (`modernbert-ft-v3` CPU ceiling)
  4. At-least-once recovery: an unacknowledged `inflight` batch is replayed first
  5. Backpressure (`429`) and Redis outage (`503` on `/ingest` and `/readyz`)

Usage:
    uv run python tools/smoke_redis_queue.py
    uv run python tools/smoke_redis_queue.py --redis-url redis://localhost:6379/15
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "services"))
sys.path.insert(0, str(REPO_ROOT / "tools"))

from common.redis_queue import RedisLogQueue  # noqa: E402
from common.wire import SCHEMA_VERSION  # noqa: E402

VALID_TOKEN = "awa_redis_test_token"


class InMemoryRedis:
    """Thread-safe subset of Redis list + Lua semantics used by `RedisLogQueue`."""

    def __init__(self) -> None:
        self._lists: dict[str, list[str]] = {}
        self._cond = threading.Condition()
        self.down = False

    def _check_up(self) -> None:
        if self.down:
            raise ConnectionError("Simulated Redis connection failure")

    def ping(self) -> bool:
        self._check_up()
        return True

    def rpush(self, key: str, *values: str) -> int:
        with self._cond:
            self._check_up()
            bucket = self._lists.setdefault(key, [])
            bucket.extend(str(v) for v in values)
            self._cond.notify_all()
            return len(bucket)

    def lpop(self, key: str) -> str | None:
        with self._cond:
            self._check_up()
            bucket = self._lists.get(key)
            if not bucket:
                return None
            val = bucket.pop(0)
            if not bucket:
                self._lists.pop(key, None)
            return val

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        with self._cond:
            self._check_up()
            bucket = list(self._lists.get(key, []))
            if end == -1:
                return bucket[start:]
            return bucket[start : end + 1]

    def llen(self, key: str) -> int:
        with self._cond:
            self._check_up()
            return len(self._lists.get(key, []))

    def delete(self, *keys: str) -> int:
        with self._cond:
            self._check_up()
            removed = 0
            for key in keys:
                if self._lists.pop(key, None) is not None:
                    removed += 1
            return removed

    def memory_usage(self, key: str) -> int:
        with self._cond:
            self._check_up()
            bucket = self._lists.get(key, [])
            return sum(len(item.encode("utf-8")) for item in bucket)

    def eval(self, _script: str, numkeys: int, *keys_and_args) -> list[str]:
        assert numkeys == 2
        src, dst = str(keys_and_args[0]), str(keys_and_args[1])
        limit = int(keys_and_args[2])
        with self._cond:
            self._check_up()
            src_list = self._lists.get(src, [])
            if not src_list or limit <= 0:
                return []
            moved = src_list[:limit]
            del src_list[:limit]
            if not src_list:
                self._lists.pop(src, None)
            self._lists.setdefault(dst, []).extend(moved)
            return moved

    def blmove(self, src: str, dst: str, timeout: float, wherefrom: str = "LEFT", whereto: str = "RIGHT") -> str | None:
        assert wherefrom == "LEFT" and whereto == "RIGHT"
        deadline = time.monotonic() + max(timeout, 0.0)
        with self._cond:
            while True:
                self._check_up()
                src_list = self._lists.get(src, [])
                if src_list:
                    item = src_list.pop(0)
                    if not src_list:
                        self._lists.pop(src, None)
                    self._lists.setdefault(dst, []).append(item)
                    return item
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=remaining)

    def pipeline(self, transaction: bool = False):
        del transaction
        parent = self

        class _Pipe:
            def __init__(self) -> None:
                self._ops: list[tuple[str, tuple]] = []

            def llen(self, key: str):
                self._ops.append(("llen", (key,)))
                return self

            def memory_usage(self, key: str):
                self._ops.append(("memory_usage", (key,)))
                return self

            def execute(self) -> list[int]:
                parent._check_up()
                return [getattr(parent, name)(*args) for name, args in self._ops]

        return _Pipe()


class RecordingScorer:
    """Tracks every batch size handed to `/score` so the ceiling is verified."""

    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def __call__(self, records: list[dict]) -> dict:
        self.batch_sizes.append(len(records))
        return {
            "model_key": "modernbert-ft-v3",
            "model_revision": "local",
            "threshold": 0.5,
            "results": [
                {
                    "score": 0.99 if "FAILED PASSWORD" in r["message"].upper() else 0.02,
                    "is_anomaly": "FAILED PASSWORD" in r["message"].upper(),
                    "template_id": r.get("template_id"),
                }
                for r in records
            ],
        }


def wire_record(message: str, **overrides) -> dict:
    base = {
        "schema_version": SCHEMA_VERSION,
        "host": "web1",
        "source": "sshd",
        "message": message,
        "level": "INFO",
    }
    base.update(overrides)
    return base


def _noop_lifespan(app):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def noop(_app):
        yield

    return noop(app)


def build_ingest_client(queue: RedisLogQueue, backpressure_records: int = 80_000):
    from fastapi.testclient import TestClient

    import services.ingest.app as ingest_app
    from services.ingest.config import Settings

    settings = Settings(
        spool_max_records=queue.max_records,
        backpressure_records=backpressure_records,
        retry_after_s=30,
        queue_backend="redis",
    )

    class StubTokens:
        def resolve(self, token: str) -> str | None:
            return "web1-agent" if token == VALID_TOKEN else None

        def touch(self, agent_id: str) -> None:
            pass

    ingest_app._state.update({"spool": queue, "tokens": StubTokens(), "settings": settings, "error": None})
    client = TestClient(ingest_app.app, raise_server_exceptions=True)
    client.app.router.lifespan_context = _noop_lifespan
    return client


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    return condition


def make_queue(redis_url: str | None, key_prefix: str, max_records: int = 100_000):
    if redis_url:
        q = RedisLogQueue(redis_url=redis_url, key_prefix=key_prefix, max_records=max_records)
        q._client.delete(q.pending_key, q.inflight_key)
        return q, None
    fake = InMemoryRedis()
    return RedisLogQueue(key_prefix=key_prefix, max_records=max_records, client=fake), fake


def main() -> int:
    argp = argparse.ArgumentParser(description=__doc__)
    argp.add_argument("--redis-url", default=None, help="Optional live Redis URL to run against")
    args = argp.parse_args()

    failures = 0
    auth = {"Authorization": f"Bearer {VALID_TOKEN}"}
    prefix = f"anomaly_watch:test_{int(time.time() * 1000)}"

    print("\n1. ingest -> Redis queue contract")
    queue, fake = make_queue(args.redis_url, f"{prefix}:c1")
    client = build_ingest_client(queue)

    unauth = client.post("/ingest", json=[wire_record("GET / 200")])
    failures += not check("unauthenticated rejected (401)", unauth.status_code == 401)
    failures += not check("nothing queued on 401", queue.stats().records == 0)

    resp = client.post("/ingest", json=[wire_record(f"Accepted publickey {i}") for i in range(5)], headers=auth)
    failures += not check("FluentBit bare array queued (202)", resp.status_code == 202 and resp.json()["accepted"] == 5)
    status_body = client.get("/status").json()
    failures += not check(
        "/status reports redis queue depth",
        status_body["queue"]["backend"] == "redis"
        and status_body["queue"]["pending_records"] == 5
        and status_body["spool"]["records"] == 5,
        str(status_body["queue"]),
    )

    print("\n2. greedy non-waiting batch claim (ceiling, never forced wait)")
    started = time.perf_counter()
    batch = queue.claim_batch(max_records=128, timeout_s=2.0)
    elapsed_ms = (time.perf_counter() - started) * 1000
    failures += not check("5 records returned immediately without waiting for 128", len(batch) == 5, f"{len(batch)} items")
    failures += not check("returned in < 200ms despite timeout_s=2.0", elapsed_ms < 200.0, f"{elapsed_ms:.1f}ms")
    failures += not check("agent_id stamped in queued payload", batch[0].get("agent_id") == "web1-agent")
    queue.ack_batch()
    failures += not check("queue empty after ack_batch()", queue.stats().records == 0)

    # Verify reactive BLMOVE wakeup when idle:
    arrived: list[list[dict]] = []
    t_start = time.perf_counter()

    def _consumer() -> None:
        arrived.append(queue.claim_batch(max_records=128, timeout_s=3.0))

    worker = threading.Thread(target=_consumer, daemon=True)
    worker.start()
    time.sleep(0.06)
    client.post("/ingest", json=[wire_record("line A"), wire_record("line B"), wire_record("line C")], headers=auth)
    worker.join(timeout=2.0)
    wake_ms = (time.perf_counter() - t_start) * 1000
    failures += not check(
        "idle BLMOVE wakes reactively with all 3 records",
        bool(arrived) and len(arrived[0]) == 3 and wake_ms < 1000.0,
        f"got {len(arrived[0]) if arrived else 0} in {wake_ms:.1f}ms",
    )
    queue.ack_batch()

    print("\n3. burst ceiling (128 from Redis) + detector sub-batch ceiling (32 to /score)")
    sys.path.insert(0, str(REPO_ROOT / "services" / "collector"))
    from cache import ScoreCache
    from parser import LogParser
    from pipeline import Pipeline
    from services.collector.config import Settings as CollectorSettings

    queue, _ = make_queue(args.redis_url, f"{prefix}:c3")
    client = build_ingest_client(queue)
    for chunk_start in range(0, 300, 60):
        client.post(
            "/ingest",
            json=[
                wire_record(f"Failed password for invalid user user{chunk_start + i} from 10.0.1.{(chunk_start + i) % 250}")
                for i in range(60)
            ],
            headers=auth,
        )
    failures += not check("300 records queued across 5 agent flushes", queue.stats().pending_records == 300)

    c_settings = CollectorSettings()
    failures += not check(
        "collector defaults tuned to model (claim=128, score=32)",
        c_settings.queue_batch_size == 128 and c_settings.batch_size == 32,
        f"claim={c_settings.queue_batch_size}, score={c_settings.batch_size}",
    )
    scorer = RecordingScorer()
    pipeline = Pipeline(LogParser(), ScoreCache(), c_settings, scorer)

    claimed_sizes: list[int] = []
    total_anomalies = 0
    while True:
        claimed = queue.claim_batch(max_records=c_settings.queue_batch_size, timeout_s=0.0)
        if not claimed:
            break
        claimed_sizes.append(len(claimed))
        outcome = pipeline.process(claimed)
        total_anomalies += len(outcome.anomalies)
        queue.ack_batch()

    failures += not check("drained in chunks of [128, 128, 44]", claimed_sizes == [128, 128, 44], str(claimed_sizes))
    failures += not check(
        "every /score call <= 32 records",
        bool(scorer.batch_sizes) and max(scorer.batch_sizes) <= 32,
        f"score batches={scorer.batch_sizes}",
    )
    failures += not check("all 300 brute-force lines classified", total_anomalies == 300, f"{total_anomalies} rows")

    print("\n4. at-least-once crash recovery (inflight replay)")
    queue, _ = make_queue(args.redis_url, f"{prefix}:c4")
    client = build_ingest_client(queue)
    client.post("/ingest", json=[wire_record(f"first-{i}") for i in range(10)], headers=auth)
    first_claim = queue.claim_batch(max_records=128, timeout_s=0.0)
    # Simulate crash BEFORE ack_batch(), while new traffic continues arriving:
    client.post("/ingest", json=[wire_record(f"later-{i}") for i in range(4)], headers=auth)
    mid_stats = queue.stats()
    failures += not check(
        "unacked batch tracked in inflight",
        mid_stats.inflight_records == 10 and mid_stats.pending_records == 4 and mid_stats.records == 14,
        str(mid_stats),
    )
    replayed = queue.claim_batch(max_records=128, timeout_s=0.0)
    failures += not check(
        "crash recovery replays exact inflight batch first",
        [r["message"] for r in replayed] == [r["message"] for r in first_claim],
    )
    queue.ack_batch()
    next_batch = queue.claim_batch(max_records=128, timeout_s=0.0)
    failures += not check("subsequent pending records intact", len(next_batch) == 4)
    queue.ack_batch()

    print("\n5. backpressure (429) and Redis outage resilience (503)")
    queue, fake = make_queue(args.redis_url, f"{prefix}:c5", max_records=25)
    client = build_ingest_client(queue, backpressure_records=10)
    client.post("/ingest", json=[wire_record(f"bp-{i}") for i in range(12)], headers=auth)
    throttled = client.post("/ingest", json=[wire_record("overflow")], headers=auth)
    failures += not check("saturated Redis queue returns 429", throttled.status_code == 429)
    failures += not check("Retry-After header set", throttled.headers.get("Retry-After") == "30")

    if fake is not None:
        fake.down = True
        outage_ingest = client.post("/ingest", json=[wire_record("during outage")], headers=auth)
        outage_ready = client.get("/readyz")
        failures += not check("Redis outage returns 503 on POST /ingest", outage_ingest.status_code == 503)
        failures += not check("Redis outage flips /readyz to 503", outage_ready.status_code == 503)
        fake.down = False
        recovered_ready = client.get("/readyz")
        failures += not check("/readyz recovers to 200 when Redis returns", recovered_ready.status_code == 200)
    else:
        queue._client.delete(queue.pending_key, q.inflight_key if "q" in locals() else queue.inflight_key)

    print()
    if failures:
        print(f"FAIL: {failures} check(s) failed")
        return 1
    print("PASS: Redis queue, greedy batching, and ingest/collector contracts hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
