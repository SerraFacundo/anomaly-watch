"""ingest-service configuration. Environment variables only.

Same convention as `services/detector/config.py`: nothing tunable is hardcoded,
because the numbers here are the ones an operator has to change at 3am when the
pipeline is behind.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else int(raw)


@dataclass(frozen=True)
class Settings:
    spool_dir: Path = REPO_ROOT / "data" / "spool"

    # Bound on the backlog held on the central box. Sized from the measured
    # detector rate: 100k records is roughly an hour of buffer at the peak the
    # pipeline is designed for, which is long enough to ride out a collector
    # restart and short enough not to fill the disk during a real outage.
    spool_max_records: int = 100_000

    # Backpressure fires before the spool is actually full, so agents start
    # holding records while there is still room to absorb an in-flight batch.
    backpressure_records: int = 80_000
    retry_after_s: int = 30

    dsn: str = "postgresql://anomaly:anomaly@postgres:5432/anomaly_watch"

    # Tokens are looked up per request. Postgres round trip per batch would put
    # the auth store in the hot path; a short TTL keeps revocation reasonably
    # prompt without doing that.
    token_cache_ttl_s: int = 60

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            spool_dir=Path(os.environ.get("INGEST_SPOOL_DIR", REPO_ROOT / "data" / "spool")),
            spool_max_records=_int("INGEST_SPOOL_MAX_RECORDS", 100_000),
            backpressure_records=_int("INGEST_BACKPRESSURE_RECORDS", 80_000),
            retry_after_s=_int("INGEST_RETRY_AFTER_S", 30),
            dsn=os.environ.get("ANOMALY_WATCH_DSN")
            or "postgresql://anomaly:anomaly@postgres:5432/anomaly_watch",
            token_cache_ttl_s=_int("INGEST_TOKEN_CACHE_TTL_S", 60),
        )
