"""Analysis latency: stamping, per-minute histograms and reporting to AnomalyWatch.

The receiving contract (bucket bounds, payload, idempotency) is owned by the
AnomalyWatch backend — change `add-response-time-metric`, TDD_0016. This module
consumes it; it does not define it.
"""

from __future__ import annotations

import time
from bisect import bisect_left
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

# Must match LatencyBuckets.BoundsMs in AnomalyWatch. A mismatch is rejected with
# a 400 that names the expected bounds; change them on that side first.
BOUNDS_MS: tuple[int, ...] = (
    50, 100, 250, 500, 750, 1000, 1500, 2000, 3000, 4000, 5000, 7500,
    10000, 15000, 20000, 30000, 45000, 60000, 120000,
)
STAGE_ANALYSIS = "ANALYSIS"

DEFAULT_BUFFER_SIZE = 60
DEFAULT_TIMEOUT_S = 2.0


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Stamp:
    """A wall-clock instant for the record, plus a monotonic reading for durations.

    The monotonic reading never leaves the process: it only exists so that a
    wall-clock step (NTP, manual change) cannot bend a measured duration.
    """

    wall: datetime
    mono: float

    @classmethod
    def now(cls) -> Stamp:
        return cls(utc_now(), time.perf_counter())

    def elapsed_ms_until(self, later: Stamp) -> float:
        return max(0.0, (later.mono - self.mono) * 1000)


def minute_of(instant: datetime) -> datetime:
    return instant.astimezone(UTC).replace(second=0, microsecond=0)


@dataclass
class LatencyWindow:
    start: datetime
    count: int = 0
    sum_ms: float = 0.0
    max_ms: float = 0.0
    bucket_counts: list[int] = field(default_factory=lambda: [0] * (len(BOUNDS_MS) + 1))

    def add(self, duration_ms: float) -> None:
        # bisect_left puts a value equal to a bound in that bound's bucket: (lower, upper].
        self.bucket_counts[bisect_left(BOUNDS_MS, duration_ms)] += 1
        self.count += 1
        self.sum_ms += duration_ms
        self.max_ms = max(self.max_ms, duration_ms)

    def to_payload(self) -> dict:
        return {
            "stage": STAGE_ANALYSIS,
            "windowStart": self.start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "count": self.count,
            "sumMs": round(self.sum_ms, 3),
            "maxMs": round(self.max_ms, 3),
            "bucketCounts": list(self.bucket_counts),
        }


class WindowAggregator:
    """Assigns each sample to the UTC minute in which its analysis completed."""

    def __init__(self) -> None:
        self._open: dict[datetime, LatencyWindow] = {}

    def add(self, duration_ms: float, analyzed_at: datetime) -> None:
        start = minute_of(analyzed_at)
        window = self._open.get(start)
        if window is None:
            window = self._open[start] = LatencyWindow(start)
        window.add(duration_ms)

    def close_due(self, now: datetime) -> list[LatencyWindow]:
        """Close every window whose minute has already passed, even with no new samples."""
        current = minute_of(now)
        due = sorted(start for start in self._open if start < current)
        return [self._open.pop(start) for start in due]

    def close_all(self) -> list[LatencyWindow]:
        closed = [self._open[start] for start in sorted(self._open)]
        self._open.clear()
        return closed


class Reporter:
    """Sends closed windows to AnomalyWatch, keeping unconfirmed ones for the next try.

    Never raises: a failed report must not stop detection. The buffer is bounded
    so a long outage costs the oldest windows (counted), not unbounded memory.
    """

    def __init__(
        self,
        client,
        endpoint: str,
        token: str,
        source: str,
        instance_id: str,
        buffer_size: int = DEFAULT_BUFFER_SIZE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        log: Callable[[str], None] = print,
    ) -> None:
        self._client = client
        self._url = f"{endpoint.rstrip('/')}/api/v1/pipeline/latency-windows"
        self._headers = {"Authorization": f"Bearer {token}"}
        self._source = source
        self._instance_id = instance_id
        self._pending: deque[LatencyWindow] = deque()
        self._buffer_size = buffer_size
        self._timeout_s = timeout_s
        self._log = log
        self.delivered = 0
        self.dropped = 0
        self.rejected = 0

    @property
    def pending(self) -> int:
        return len(self._pending)

    def enqueue(self, windows: list[LatencyWindow]) -> None:
        self._pending.extend(windows)
        overflow = len(self._pending) - self._buffer_size
        if overflow > 0:
            for _ in range(overflow):
                self._pending.popleft()
            self.dropped += overflow
            self._log(f"latency: buffer full, dropped {overflow} oldest window(s) ({self.dropped} so far)")

    def flush(self) -> None:
        if not self._pending:
            return

        windows = list(self._pending)
        payload = {
            "source": self._source,
            "instanceId": self._instance_id,
            "boundsMs": list(BOUNDS_MS),
            "windows": [window.to_payload() for window in windows],
        }

        try:
            response = self._client.post(self._url, json=payload, headers=self._headers, timeout=self._timeout_s)
        except Exception as exc:  # noqa: BLE001 — any transport failure means "retry later"
            self._log(f"latency: report failed ({exc.__class__.__name__}); {len(windows)} window(s) kept for retry")
            return

        if response.is_success:
            self.delivered += len(windows)
            self._pending.clear()
        elif 400 <= response.status_code < 500:
            # Resending the same payload would be rejected the same way.
            self.rejected += len(windows)
            self._pending.clear()
            self._log(f"latency: report rejected ({response.status_code}): {response.text[:500]}")
        else:
            self._log(f"latency: report failed ({response.status_code}); {len(windows)} window(s) kept for retry")

    def summary(self) -> str:
        return (
            f"{self.delivered} window(s) reported, {self.pending} undelivered, "
            f"{self.dropped} dropped, {self.rejected} rejected"
        )
