"""log-collector configuration. Environment variables only.

Same convention as the other two services. The shedding thresholds in
particular are here rather than in code because they are what an operator has
to move when the pipeline is behind and the dashboard is going quiet.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else int(raw)


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else float(raw)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    spool_dir: Path = REPO_ROOT / "data" / "spool"
    detector_endpoint: str = "http://detector:8003"
    dsn: str = "postgresql://anomaly:anomaly@postgres:5432/anomaly_watch"

    # Drain3 learns online, so this file is what keeps a template id meaning the
    # same thing after a restart. Without it every restart renumbers every
    # template and the historical anomaly rows stop being interpretable.
    drain_state_path: Path = REPO_ROOT / "data" / "spool" / "drain3.state"

    # Inference sub-batch ceiling sent to `POST /score`. Sized from measured
    # modernbert-ft-v3 CPU latency (~3.0s at 32 records on 2 threads) and
    # dynamic token padding (`padding=True` up to 256 tokens): beyond 32 on
    # 2 CPU threads throughput no longer improves while padding waste and
    # per-request blocking latency grow linearly.
    batch_size: int = 32

    # Queue backend and greedy per-cycle claim ceiling (1..queue_batch_size,
    # never waiting to accumulate a full batch).
    queue_backend: str = "spool"
    redis_url: str = "redis://redis:6379/0"
    redis_queue_key: str = "anomaly_watch:logs"
    queue_batch_size: int = 128

    cache_enabled: bool = True
    cache_max_entries: int = 10_000  # 2,409 templates covered 119k lines of real journal
    cache_ttl_s: float = 3600.0

    # Shedding stages, in the order design.md D4 fixes them. Depth is queued
    # records pending + inflight.
    shed_sample_at: int = 20_000
    shed_level_at: int = 50_000
    sample_one_in: int = 10
    hot_template_hits: int = 50  # only well-established, never-flagged templates

    raw_rate: float = 29.6  # ml/eval/results/modernbert-ft.json, for the effective-rate projection
    poll_interval_s: float = 0.5

    @classmethod
    def from_env(cls) -> "Settings":
        spool = Path(os.environ.get("COLLECTOR_SPOOL_DIR", REPO_ROOT / "data" / "spool"))
        return cls(
            spool_dir=spool,
            detector_endpoint=os.environ.get("COLLECTOR_DETECTOR_ENDPOINT", "http://detector:8003"),
            dsn=os.environ.get("ANOMALY_WATCH_DSN")
            or "postgresql://anomaly:anomaly@postgres:5432/anomaly_watch",
            drain_state_path=Path(os.environ.get("COLLECTOR_DRAIN_STATE", spool.parent / "drain3.state")),
            batch_size=_int("COLLECTOR_BATCH_SIZE", 32),
            queue_backend=(os.environ.get("QUEUE_BACKEND") or "redis").strip().lower(),
            redis_url=os.environ.get("REDIS_URL") or "redis://redis:6379/0",
            redis_queue_key=os.environ.get("REDIS_QUEUE_KEY") or "anomaly_watch:logs",
            queue_batch_size=_int("COLLECTOR_QUEUE_BATCH_SIZE", 128),
            cache_enabled=_bool("COLLECTOR_CACHE_ENABLED", True),
            cache_max_entries=_int("COLLECTOR_CACHE_MAX_ENTRIES", 10_000),
            cache_ttl_s=_float("COLLECTOR_CACHE_TTL_S", 3600.0),
            shed_sample_at=_int("COLLECTOR_SHED_SAMPLE_AT", 20_000),
            shed_level_at=_int("COLLECTOR_SHED_LEVEL_AT", 50_000),
            sample_one_in=_int("COLLECTOR_SAMPLE_ONE_IN", 10),
            hot_template_hits=_int("COLLECTOR_HOT_TEMPLATE_HITS", 50),
            raw_rate=_float("COLLECTOR_RAW_RATE", 29.6),
            poll_interval_s=_float("COLLECTOR_POLL_INTERVAL_S", 0.5),
        )
