"""Detector service configuration. Everything tunable is an environment variable.

Nothing here is hardcoded on purpose: promoting a model must cost one config
value and a restart, never a code change.
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
    model_key: str | None = None
    revision_override: str | None = None
    threshold: float = 0.5
    max_batch_size: int = 256
    max_length: int = 256
    num_threads: int | None = None
    anomaly_index_override: int | None = None
    cache_dir: Path = REPO_ROOT / "models" / "cache"
    # Deployed stacks run offline so no runtime egress can leak log data or
    # silently pull a different model than the one that was evaluated.
    offline: bool = True
    results_dir: Path = REPO_ROOT / "ml" / "eval" / "results"

    @classmethod
    def from_env(cls) -> "Settings":
        raw_index = os.environ.get("DETECTOR_ANOMALY_INDEX")
        return cls(
            model_key=os.environ.get("DETECTOR_MODEL_KEY") or None,
            revision_override=os.environ.get("DETECTOR_MODEL_REVISION") or None,
            threshold=_float("DETECTOR_THRESHOLD", 0.5),
            max_batch_size=_int("DETECTOR_MAX_BATCH_SIZE", 256),
            max_length=_int("DETECTOR_MAX_LENGTH", 256),
            num_threads=_int("DETECTOR_NUM_THREADS", 0) or None,
            anomaly_index_override=int(raw_index) if raw_index else None,
            cache_dir=Path(os.environ.get("ANOMALY_WATCH_MODEL_CACHE", REPO_ROOT / "models" / "cache")),
            offline=_bool("HF_HUB_OFFLINE", True),
        )
