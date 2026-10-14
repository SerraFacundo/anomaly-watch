"""Analysis-latency buckets, shared by the collector that fills them and the
bridge that reports them.

The bounds are owned by the web app (`LatencyBuckets.BoundsMs`, TDD_0016). A
report built on other bounds is rejected with a 400 that names the expected
ones, so change them on that side first.
"""

from __future__ import annotations

from bisect import bisect_left

BOUNDS_MS: tuple[int, ...] = (
    50, 100, 250, 500, 750, 1000, 1500, 2000, 3000, 4000, 5000, 7500,
    10000, 15000, 20000, 30000, 45000, 60000, 120000,
)
# One count per bound, plus the open bucket above the last one.
BUCKETS = len(BOUNDS_MS) + 1


def bucket_of(duration_ms: float) -> int:
    """Index of the bucket a sample falls in. Buckets are (lower, upper]."""
    return bisect_left(BOUNDS_MS, duration_ms)


if __name__ == "__main__":
    assert BOUNDS_MS[bucket_of(1000)] == 1000, "a sample on a bound belongs to that bound"
    assert bucket_of(1000.001) == bucket_of(1000) + 1
    assert bucket_of(0) == 0
    assert bucket_of(120001) == BUCKETS - 1, "above the last bound is the open bucket"
    print("PASS: bucket assignment")
