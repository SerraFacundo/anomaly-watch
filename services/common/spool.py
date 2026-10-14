"""The durability boundary between `ingest-service` and `log-collector`.

`POST /ingest` may only acknowledge a batch once that batch will survive a
restart. An in-memory queue cannot promise that: the process dies, the records
go with it, and the agent has already deleted its copy because it was told the
batch landed. So the handoff is a directory of files on a shared volume.

This is a deliberately small piece of a broker, and only the piece that is
needed: durable append, ordered read, delete after processing. There is no
replay, no consumer group, no retention window — `design.md` D1 explains why
none of those are being bought.

Writes are atomic by rename. A reader only ever sees a `.jsonl` file that was
completely written and fsynced, so a crash mid-write leaves a `.tmp` behind and
never a half-batch that parses as a short one.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SPOOL_SUFFIX = ".jsonl"
TEMP_SUFFIX = ".tmp"


class SpoolFull(RuntimeError):
    """Raised when the spool is over its bound. Becomes a 429, not a 500.

    A full spool means the collector is behind, which is a backpressure signal,
    not a failure. The agent keeps the records on its own disk and retries.
    """


@dataclass(frozen=True)
class SpoolStats:
    files: int
    records: int
    bytes: int


class Spool:
    """Append-only batch spool shared by writer and reader over a volume.

    Record count is encoded in the filename so queue depth is a directory
    listing rather than a read of every pending file — depth is checked on every
    single ingest request, and opening the backlog to measure the backlog is how
    an overloaded system gets more overloaded.
    """

    def __init__(self, directory: Path, max_records: int = 100_000) -> None:
        self.directory = directory
        self.max_records = max_records
        self.directory.mkdir(parents=True, exist_ok=True)
        self._seq = 0

    # -- writer side -------------------------------------------------------

    def append(self, records: list[dict]) -> Path:
        """Durably write one batch. Returns once the data is on disk.

        Raises SpoolFull if the backlog is already over its bound, so the
        caller can shed rather than let an unbounded backlog consume the host
        disk of the machine that is also running the model.
        """
        if not records:
            raise ValueError("Refusing to spool an empty batch.")

        stats = self.stats()
        if stats.records >= self.max_records:
            raise SpoolFull(
                f"Spool holds {stats.records:,} records, at its bound of "
                f"{self.max_records:,}. The collector is behind."
            )

        self._seq += 1
        stem = f"batch-{time.time_ns():019d}-{self._seq:06d}-{len(records)}"
        temp = self.directory / f"{stem}{TEMP_SUFFIX}"
        final = self.directory / f"{stem}{SPOOL_SUFFIX}"

        payload = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            # flush() only reaches the OS buffer. Without fsync the ack would be
            # a promise made about data that is still only in page cache.
            os.fsync(handle.fileno())
        temp.rename(final)
        return final

    # -- reader side -------------------------------------------------------

    def pending(self) -> list[Path]:
        """Complete batches, oldest first. Timestamped names sort chronologically."""
        return sorted(self.directory.glob(f"*{SPOOL_SUFFIX}"))

    def read(self, path: Path) -> list[dict]:
        records = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    def done(self, path: Path) -> None:
        """Delete a batch after it has been fully processed.

        Called after processing, never before: a crash mid-batch replays that
        batch rather than losing it. At-least-once, and the duplicate costs one
        cache hit.
        """
        path.unlink(missing_ok=True)

    # -- both sides --------------------------------------------------------

    def stats(self) -> SpoolStats:
        files = self.pending()
        records = 0
        size = 0
        for path in files:
            records += _records_in_name(path)
            try:
                size += path.stat().st_size
            except OSError:
                # Raced with the reader deleting it. A slightly stale size is
                # fine; failing a health check over it is not.
                pass
        return SpoolStats(files=len(files), records=records, bytes=size)

    def sweep_temp(self, older_than_s: float = 300.0) -> int:
        """Remove `.tmp` files left by a crash mid-write. Returns how many."""
        cutoff = time.time() - older_than_s
        removed = 0
        for path in self.directory.glob(f"*{TEMP_SUFFIX}"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                pass
        return removed


def received_at(path: Path) -> datetime | None:
    """When `append` took the batch: the `time_ns` field of its name, in UTC.

    None for a name `append` did not write, so the caller can fall back instead
    of failing a batch over its filename.
    """
    try:
        return datetime.fromtimestamp(int(path.stem.split("-")[1]) / 1e9, UTC)
    except (IndexError, ValueError, OverflowError, OSError):
        return None


def _records_in_name(path: Path) -> int:
    """Record count is the last dash-separated field of the stem."""
    try:
        return int(path.stem.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0
