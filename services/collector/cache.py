"""Template-keyed score cache — the mechanism that closes the throughput gap.

`detector-service` scores 29.6 records/s on CPU. Peak PyME load is 50–200
lines/s. The cache is what makes those numbers compatible: logs repeat, so each
distinct Drain3 template is scored once and every repeat is served for free.

Measured on 118,817 lines of real systemd journal (`tasks.md` 4.3): 2,409
distinct templates, 20.3 per 1,000 lines, a 97.97% hit ceiling that holds flat
in steady state. Worst single source was 96.48%.

The assumption underneath it — that template identity implies score identity —
is a good default and a bad guarantee, which is what `_BYPASS_PATTERNS` is for.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from dataclasses import dataclass

# Records whose meaning is not determined by their template. Drain3 clusters by
# textual similarity, so at the configured threshold of 0.4 it will happily put
# "Failed password for <*> from <*>" and "Accepted password for <*> from <*>"
# in the same cluster — and the difference between those two lines is the entire
# security signal. Anything matching here is always scored.
_BYPASS_PATTERNS = [
    # Authentication outcomes.
    re.compile(r"\b(fail(ed|ure)?|invalid|unauthorized|denied|refused|accepted|success(ful)?)\b"
               r".{0,40}\b(password|auth|login|user|session|key|token|credential)\b", re.IGNORECASE),
    re.compile(r"\b(authentication|authorization|sudo|su|sshd|pam_\w+)\b", re.IGNORECASE),
    # HTTP 5xx. Anchored to where a status code actually appears, never to a bare
    # 5xx-looking number: an access log is full of three-digit response sizes, and
    # matching those made 12.65% of real nginx traffic bypass the cache instead of
    # ~1%. Measured on 30k lines of production e-commerce traffic.
    re.compile(r'"\s+5\d{2}\b'),  # combined log format: "GET / HTTP/1.1" 500 1234
    re.compile(r"\b(status|code|response)\b\W{0,3}5\d{2}\b", re.IGNORECASE),
    re.compile(r"\b5\d{2}\s+(internal server error|service unavailable|bad gateway|gateway time)",
               re.IGNORECASE),
    # Non-zero exit / return codes.
    re.compile(r"\b(exit|exited|status|code|errno|rc)\b[=: ]+\s*(?!0\b)-?\d+", re.IGNORECASE),
    # Kernel-level severity.
    re.compile(r"\b(panic|oops|bug:|segfault|general protection|out of memory|oom[- ]killer|"
               r"call trace|kernel bug|hung task|i/o error|filesystem error)\b", re.IGNORECASE),
]


def needs_bypass(message: str, params: list[str] | None = None) -> bool:
    """True when the record must be scored regardless of any cached entry."""
    haystack = message if not params else message + " " + " ".join(params)
    return any(pattern.search(haystack) for pattern in _BYPASS_PATTERNS)


@dataclass
class _Entry:
    score: float
    is_anomaly: bool
    stored_at: float
    hits: int = 0


@dataclass
class CacheStats:
    """Everything an operator needs to tell a working cache from a dead one."""

    lookups: int = 0
    hits: int = 0
    misses: int = 0
    bypasses: int = 0
    expired: int = 0
    entries: int = 0
    evictions: int = 0
    # Records that missed the cache but shared a template with another record in
    # the same batch, so one inference covered them all. Counted separately from
    # `hits` because they were genuine misses at lookup time — but they avoided
    # an inference just the same, and the hit rate exists to measure inference
    # avoided, not lookups satisfied.
    coalesced: int = 0

    @property
    def hit_rate(self) -> float:
        """Fraction of records that did not cost an inference."""
        return (self.hits + self.coalesced) / self.lookups if self.lookups else 0.0

    @property
    def bypass_rate(self) -> float:
        return self.bypasses / self.lookups if self.lookups else 0.0

    def effective_rate(self, raw_rate: float) -> float:
        """Records/s the pipeline can sustain given the observed hit rate."""
        miss_rate = max(1.0 - self.hit_rate, 1e-9)
        return raw_rate / miss_rate

    def as_dict(self, raw_rate: float) -> dict:
        return {
            "lookups": self.lookups,
            "hits": self.hits,
            "misses": self.misses,
            "bypasses": self.bypasses,
            "expired": self.expired,
            "coalesced": self.coalesced,
            "entries": self.entries,
            "evictions": self.evictions,
            "hit_rate": round(self.hit_rate, 4),
            "bypass_rate": round(self.bypass_rate, 4),
            "effective_records_per_s": round(self.effective_rate(raw_rate), 1),
        }


class ScoreCache:
    """LRU over `(model_key, model_revision, template_id)`.

    Model identity is part of the key rather than a separate invalidation step,
    so a model swap cannot serve a stale score even if nobody remembered to
    flush anything. That matters more than it looks: the whole point of storing
    `model_key` and `model_revision` on every anomaly row is that a score is
    meaningless without knowing what produced it, and a cache that leaks scores
    across a swap would forge exactly that provenance.

    Default capacity of 10,000 comes from the measurement, not from a round
    number: 2,409 templates covered 119k lines of real journal.
    """

    def __init__(self, max_entries: int = 10_000, ttl_s: float = 3600.0, enabled: bool = True) -> None:
        self.max_entries = max_entries
        self.ttl_s = ttl_s
        self.enabled = enabled
        self._entries: OrderedDict[tuple[str, str, str], _Entry] = OrderedDict()
        self.stats = CacheStats()

    def get(
        self,
        model_key: str,
        model_revision: str,
        template_id: str | None,
        message: str,
        params: list[str] | None = None,
    ) -> tuple[str, _Entry | None]:
        """Classify a record: ("hit", entry), ("miss", None) or ("bypass", None).

        A miss and a bypass are both "call the detector", but they are not the
        same thing and the caller must tell them apart: misses of the same
        template can share one inference, while bypassed records each need their
        own because the whole reason they bypassed is that their template does
        not determine their score.
        """
        self.stats.lookups += 1

        if not self.enabled or template_id is None:
            # No template id means Drain3 did not cluster the line. Nothing to
            # key on, so it is a miss by construction.
            self.stats.misses += 1
            return "miss", None

        if needs_bypass(message, params):
            self.stats.bypasses += 1
            return "bypass", None

        key = (model_key, model_revision, template_id)
        entry = self._entries.get(key)
        if entry is None:
            self.stats.misses += 1
            return "miss", None

        if time.monotonic() - entry.stored_at > self.ttl_s:
            # A template's meaning drifts: the same shape of line can go from
            # routine to symptomatic as a system degrades.
            del self._entries[key]
            self.stats.expired += 1
            self.stats.misses += 1
            return "miss", None

        entry.hits += 1
        self._entries.move_to_end(key)
        self.stats.hits += 1
        return "hit", entry

    def put(
        self, model_key: str, model_revision: str, template_id: str | None, score: float, is_anomaly: bool
    ) -> None:
        if not self.enabled or template_id is None:
            return
        key = (model_key, model_revision, template_id)
        self._entries[key] = _Entry(score=score, is_anomaly=is_anomaly, stored_at=time.monotonic())
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
            self.stats.evictions += 1
        self.stats.entries = len(self._entries)

    def hot_templates(self, minimum_hits: int) -> set[str]:
        """Template ids seen often and scored below threshold.

        These are the only records the drop policy is allowed to sample: a
        template that has been scored many times and never flagged is the
        cheapest thing in the stream to lose.
        """
        return {
            key[2]
            for key, entry in self._entries.items()
            if entry.hits >= minimum_hits and not entry.is_anomaly
        }

    def snapshot(self, raw_rate: float) -> dict:
        self.stats.entries = len(self._entries)
        return {"enabled": self.enabled, "max_entries": self.max_entries, "ttl_s": self.ttl_s,
                **self.stats.as_dict(raw_rate)}
