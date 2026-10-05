"""Optional raw-log archive, for deployments that must keep everything.

Off by default. `design.md` D7 discards normal lines after scoring because a
year of raw logs in Postgres does not fit the hardware this product is sold to
run on. Some customers have a compliance obligation that does not care about
that, so this writes the raw stream to rotated gzip files on disk instead —
outside the database, outside the dashboard, grep-and-forensics only.

The trade-off is stated plainly rather than hidden behind a flag name: turning
this on means the host needs disk for roughly a twentieth of the uncompressed
log volume, forever, and nothing in the product will read it back.
"""

from __future__ import annotations

import gzip
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("collector.archive")


class RawArchive:
    """Append-only gzip JSONL, one file per UTC day."""

    def __init__(self, directory: Path | None) -> None:
        self.directory = directory
        self.enabled = directory is not None
        self._handle = None
        self._day: str | None = None
        if self.enabled:
            directory.mkdir(parents=True, exist_ok=True)
            logger.warning(
                "Raw archive ENABLED at %s. Every log line is retained on disk; "
                "this is opt-in compliance storage, not a product feature.",
                directory,
            )

    def write(self, records: list[dict]) -> None:
        if not self.enabled or not records:
            return
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day != self._day:
            self._rotate(day)
        for record in records:
            self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        # Flushed per batch, not per line: an archive that fsyncs every line
        # would become the pipeline's bottleneck instead of the model.
        self._handle.flush()

    def _rotate(self, day: str) -> None:
        self.close()
        path = self.directory / f"raw-{day}.jsonl.gz"
        self._handle = gzip.open(path, "at", encoding="utf-8")
        self._day = day

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
