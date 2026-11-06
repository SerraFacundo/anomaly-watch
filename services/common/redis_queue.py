"""Durable Redis log queue between `ingest-service` and `log-collector`.

Replaces the shared-filesystem spool in containerised deployments while
preserving the exact operational guarantees the rest of the pipeline relies on:

  1. Bounded queue depth checked in O(1) (`LLEN`), raising `SpoolFull` when
     saturated so `POST /ingest` still signals `429` backpressure to FluentBit.
  2. Greedy, non-waiting batch consumption (`1..max_records`): if 1 record is
     queued, the consumer claims 1 record immediately without waiting to fill a
     batch; if a burst arrives, each cycle caps at `max_records`.
  3. At-least-once delivery for the single-replica collector: records move
     atomically from `<prefix>:pending` into `<prefix>:inflight` before
     scoring, and `<prefix>:inflight` is deleted only after Postgres commits.
     An interrupted batch is replayed first on the next cycle.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common.spool import Spool, SpoolFull, SpoolStats

# Atomically pop up to ARGV[1] items from KEYS[1] (pending) and push them to
# KEYS[2] (inflight), returning the moved items in FIFO order. Using a loop
# rather than `unpack()` avoids Lua's stack-size ceiling on large batches.
_CLAIM_BATCH_LUA = """
local limit = tonumber(ARGV[1])
local moved = {}
for i = 1, limit do
    local item = redis.call('LPOP', KEYS[1])
    if not item then
        break
    end
    redis.call('RPUSH', KEYS[2], item)
    moved[#moved + 1] = item
end
return moved
"""


class QueueUnavailableError(RuntimeError):
    """Raised when Redis cannot be reached. Translated to HTTP 503 at ingest."""


class RedisLogQueue:
    """Two-list (`pending` + `inflight`) reliable FIFO queue backed by Redis."""

    def __init__(
        self,
        redis_url: str = "redis://redis:6379/0",
        key_prefix: str = "anomaly_watch:logs",
        max_records: int = 100_000,
        socket_timeout_s: float = 5.0,
        client: Any | None = None,
    ) -> None:
        self.redis_url = redis_url
        self.key_prefix = key_prefix
        self.pending_key = f"{key_prefix}:pending"
        self.inflight_key = f"{key_prefix}:inflight"
        self.max_records = max_records
        self._socket_timeout_s = socket_timeout_s

        if client is not None:
            self._client = client
            self._blocking_client = client
        else:
            import redis

            self._client = redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=socket_timeout_s,
                socket_timeout=socket_timeout_s,
            )
            # Separate pool for BLMOVE so a long poll_interval_s never trips
            # the short per-command socket_timeout used by /ingest and /readyz.
            self._blocking_client = redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=socket_timeout_s,
                socket_timeout=None,
            )

    def _wrap_error(self, exc: Exception) -> QueueUnavailableError:
        return QueueUnavailableError(f"Redis queue unavailable ({self.redis_url}): {exc}")

    # -- writer side -------------------------------------------------------

    def append(self, records: list[dict]) -> int:
        """Enqueue a validated batch atomically. Raises SpoolFull at capacity."""
        if not records:
            raise ValueError("Refusing to enqueue an empty batch.")

        stats = self.stats()
        if stats.records >= self.max_records:
            raise SpoolFull(
                f"Queue holds {stats.records:,} records, at its bound of "
                f"{self.max_records:,}. The collector is behind."
            )

        encoded = [json.dumps(record, ensure_ascii=False) for record in records]
        try:
            return int(self._client.rpush(self.pending_key, *encoded))
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, (SpoolFull, ValueError)):
                raise
            raise self._wrap_error(exc) from exc

    # -- reader side -------------------------------------------------------

    def claim_batch(self, max_records: int = 128, timeout_s: float = 0.5) -> list[dict]:
        """Claim between 1 and `max_records` immediately; never waits to fill.

        1. If `inflight` is non-empty (left by a previous crash before ack),
           return it immediately so no record is lost.
        2. Drain up to `max_records` available items right away (`0ms` wait).
        3. Only if `pending` was completely empty, block up to `timeout_s` on
           `BLMOVE` for the first arrival, then greedily scoop up to
           `max_records - 1` siblings that arrived in the same push.
        """
        if max_records < 1:
            raise ValueError("max_records must be >= 1")

        try:
            existing = self._client.lrange(self.inflight_key, 0, -1)
            if existing:
                return [json.loads(raw) for raw in existing]

            moved = self._client.eval(
                _CLAIM_BATCH_LUA,
                2,
                self.pending_key,
                self.inflight_key,
                max_records,
            )
            if moved:
                return [json.loads(raw) for raw in moved]

            if timeout_s <= 0:
                return []

            first = self._blocking_client.blmove(
                self.pending_key,
                self.inflight_key,
                timeout_s,
                "LEFT",
                "RIGHT",
            )
            if first is None:
                return []

            items = [first]
            remaining = max_records - 1
            if remaining > 0:
                extra = self._client.eval(
                    _CLAIM_BATCH_LUA,
                    2,
                    self.pending_key,
                    self.inflight_key,
                    remaining,
                )
                if extra:
                    items.extend(extra)

            return [json.loads(raw) for raw in items]
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_error(exc) from exc

    def ack_batch(self) -> None:
        """Delete the `inflight` batch once scoring and persistence succeeded."""
        try:
            self._client.delete(self.inflight_key)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_error(exc) from exc

    # -- both sides --------------------------------------------------------

    def is_ready(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:  # noqa: BLE001
            return False

    def stats(self) -> SpoolStats:
        try:
            pipe = self._client.pipeline(transaction=False)
            pipe.llen(self.pending_key)
            pipe.llen(self.inflight_key)
            pending_len, inflight_len = pipe.execute()
            pending_len = int(pending_len or 0)
            inflight_len = int(inflight_len or 0)

            mem_bytes = 0
            if pending_len or inflight_len:
                try:
                    mem_pipe = self._client.pipeline(transaction=False)
                    mem_pipe.memory_usage(self.pending_key)
                    mem_pipe.memory_usage(self.inflight_key)
                    p_mem, i_mem = mem_pipe.execute()
                    mem_bytes = int(p_mem or 0) + int(i_mem or 0)
                except Exception:  # noqa: BLE001
                    mem_bytes = 0
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_error(exc) from exc

        files = (1 if pending_len > 0 else 0) + (1 if inflight_len > 0 else 0)
        return SpoolStats(
            files=files,
            records=pending_len + inflight_len,
            bytes=mem_bytes,
            pending_records=pending_len,
            inflight_records=inflight_len,
        )

    def sweep_temp(self, older_than_s: float = 300.0) -> int:
        del older_than_s
        return 0


def build_queue(
    backend: str,
    *,
    spool_dir: Path,
    redis_url: str = "redis://redis:6379/0",
    redis_queue_key: str = "anomaly_watch:logs",
    max_records: int = 100_000,
) -> Spool | RedisLogQueue:
    """Instantiate the configured queue backend (`redis` or `spool`)."""
    normalized = (backend or "redis").strip().lower()
    if normalized == "redis":
        return RedisLogQueue(
            redis_url=redis_url,
            key_prefix=redis_queue_key,
            max_records=max_records,
        )
    if normalized == "spool":
        return Spool(spool_dir, max_records=max_records)
    raise ValueError(f"Unsupported QUEUE_BACKEND={backend!r}; expected 'redis' or 'spool'.")

