"""The processing path: spooled records in, anomalies and counters out.

Kept separate from the service shell so the two things most likely to be wrong
— the cache and the drop policy — can be tested without a database, a model or
a network. Both are behaviours that only show up under load, which is exactly
when nobody wants to be discovering them.

Order of operations per record:

    parse (Drain3) -> count -> shed? -> cache lookup -> score misses -> persist

Counting happens *before* shedding on purpose. A record that gets dropped still
increments its template's hourly counter, so an operator can tell "this hour was
quiet" apart from "this hour was thrown away".
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "services"))
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "ml" / "eval"))

from baselines import _SEVERITY_PATTERN  # noqa: E402
from cache import ScoreCache, needs_bypass  # noqa: E402
from common.latency import BUCKETS, bucket_of  # noqa: E402

logger = logging.getLogger("collector.pipeline")

# Fixed, so a spool batch and a position in it always give the same anomaly_id:
# replaying the batch after a crash finds the rows it already wrote.
ANOMALY_NAMESPACE = uuid.UUID("b1f7c0de-5a3e-4c0b-8d2f-6e9a41c7d3a5")


def _now() -> datetime:
    return datetime.now(timezone.utc)

# Never shed, whatever the load. An overloaded pipeline that drops the errors is
# worse than one that falls behind on everything equally: it goes quiet exactly
# when something is wrong.
PROTECTED_LEVELS = {"ERROR", "ERR", "CRITICAL", "CRIT", "FATAL", "ALERT", "EMERG", "PANIC", "WARN", "WARNING"}
SHEDDABLE_LEVELS = {"INFO", "DEBUG", "TRACE", "NOTICE"}


# systemd reports PRIORITY as a syslog number, not a word. Mapped here rather
# than in each agent config so there is one definition of severity in the
# system, and adding a source that also speaks numbers costs nothing.
_SYSLOG_PRIORITIES = {
    "0": "EMERG", "1": "ALERT", "2": "CRIT", "3": "ERROR",
    "4": "WARN", "5": "NOTICE", "6": "INFO", "7": "DEBUG",
}


def classify_level(record: dict) -> str:
    """The record's own level if it has one, else read it out of the text.

    The severity regex is the one `ml/eval/baselines.py` already uses for the
    `level-keyword` baseline — the same notion of severity that model
    evaluation is measured against, rather than a second private definition of
    'serious' that drifts from it.
    """
    declared = (record.get("level") or "").strip().upper()
    if declared:
        # An unmapped numeric priority would land in neither the protected nor
        # the sheddable set, so it would silently become un-sheddable — the kind
        # of default that makes the drop policy quietly stop working on the one
        # source that produces most of the traffic.
        return _SYSLOG_PRIORITIES.get(declared, declared)
    return "ERROR" if _SEVERITY_PATTERN.search(record.get("message", "")) else "INFO"


@dataclass
class Counters:
    parsed: int = 0
    scored: int = 0
    from_cache: int = 0
    anomalies: int = 0
    # Rows actually written. Lower than `anomalies` once grouping collapses
    # repeats — the ratio between the two is how loud the detector is being.
    anomaly_rows: int = 0
    discarded: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def discard(self, reason: str) -> None:
        self.discarded[reason] += 1

    @property
    def total_discarded(self) -> int:
        return sum(self.discarded.values())

    def as_dict(self) -> dict:
        return {
            "parsed": self.parsed,
            "scored": self.scored,
            "from_cache": self.from_cache,
            "anomalies": self.anomalies,
            "anomaly_rows": self.anomaly_rows,
            "grouping_ratio": round(self.anomalies / self.anomaly_rows, 1) if self.anomaly_rows else 0.0,
            "discarded_total": self.total_discarded,
            "discarded_by_reason": dict(self.discarded),
        }


class ShedPolicy:
    """Load shedding, in the fixed order from `design.md` D4.

    Stage 1 samples templates that are both well-established and never flagged.
    Stage 2 drops INFO and DEBUG. Stage 3 is backpressure, and it lives in
    `ingest-service` because by then the right place for the backlog is the
    agent's own disk.

    Backpressure is deliberately last. Reaching for the agent buffer before
    shedding INFO noise spends a finite resource on records nobody would read.
    """

    def __init__(self, settings, cache: ScoreCache) -> None:
        self._settings = settings
        self._cache = cache
        self._seen: dict[str, int] = defaultdict(int)
        self.stage = 0

    def update_depth(self, depth: int) -> int:
        settings = self._settings
        if depth >= settings.shed_level_at:
            self.stage = 2
        elif depth >= settings.shed_sample_at:
            self.stage = 1
        else:
            self.stage = 0
        return self.stage

    def should_shed(self, record: dict, template_id: str | None, params: list[str] | None) -> str | None:
        """Reason to discard this record, or None to keep it."""
        if self.stage == 0:
            return None

        level = classify_level(record)
        message = record.get("message", "")

        # Two independent protections. A record is safe if its severity says so,
        # or if the cache-bypass rules say its content is security-relevant —
        # an INFO-level "Failed password" is exactly the line worth keeping.
        if level in PROTECTED_LEVELS or needs_bypass(message, params):
            return None

        if self.stage >= 2 and level in SHEDDABLE_LEVELS:
            return "level"

        if self.stage >= 1 and template_id is not None:
            if template_id in self._cache.hot_templates(self._settings.hot_template_hits):
                self._seen[template_id] += 1
                if self._seen[template_id] % self._settings.sample_one_in != 0:
                    return "sampled"
        return None


@dataclass
class Outcome:
    anomalies: list[dict]
    counters: list[dict]
    # One row per minute in which this batch had records scored.
    latency: list[dict] = field(default_factory=list)


class Pipeline:
    """Parse, cache, score, and turn the result into rows to persist.

    `scorer` is any callable taking a list of records and returning the
    `detector-service` `/score` response — the HTTP client in production, a stub
    in tests.
    """

    def __init__(self, parser, cache: ScoreCache, settings, scorer) -> None:
        self._parser = parser
        self._cache = cache
        self._settings = settings
        self._scorer = scorer
        self.counters = Counters()
        self.policy = ShedPolicy(settings, cache)
        self.model_key = "unknown"
        self.model_revision = "unknown"
        self.threshold = 0.5

    def process(
        self, wire_records: list[dict], batch: str | None = None, ingested_at: datetime | None = None
    ) -> Outcome:
        """`batch` is the spool batch's name and `ingested_at` when ingest took it.

        Both come from the spool file, so a replay passes the same two values and
        gets the same anomaly ids. Without them (tests, ad hoc runs) the batch
        gets a name of its own, never a shared one: two batches under one name
        would have the second one's anomalies dropped as replays of the first.
        """
        batch = batch or f"adhoc-{uuid.uuid4().hex}"
        ingested_at = ingested_at or _now()
        parsed: list[dict] = []
        counter_rows: dict[tuple, dict] = {}

        # Position in the spool file, counted before anything is discarded, so
        # it does not move when the shed stage differs on a replay.
        for position, wire in enumerate(wire_records):
            record = self._parser.parse(wire.get("message", ""), host=wire.get("host"))
            if record is None:
                self.counters.discard("unparseable")
                continue
            self.counters.parsed += 1

            payload = record.to_payload()
            payload.update(
                {
                    "host": wire.get("host") or payload.get("host"),
                    "agent_id": wire.get("agent_id"),
                    "source": wire.get("source"),
                    "log_timestamp": wire.get("log_timestamp") or payload.get("timestamp"),
                    "_position": position,
                }
            )

            key = (payload.get("template_id") or "-", payload.get("host") or "-")
            row = counter_rows.setdefault(
                key,
                {
                    "template_id": key[0],
                    "host": key[1],
                    "template": payload.get("template"),
                    "hour": datetime.now(timezone.utc).isoformat(),
                    "count": 0,
                    "anomaly_count": 0,
                },
            )
            # Counted before the shed check: a dropped record is still traffic.
            row["count"] += 1

            reason = self.policy.should_shed(wire, payload.get("template_id"), payload.get("params"))
            if reason:
                self.counters.discard(reason)
                continue

            parsed.append(payload)

        resolved = self._score(parsed)
        # A clock stepped backwards between ingest and here would put the
        # analysis before the ingestion, and the app rejects that anomaly.
        resolved = [(*rest, max(analyzed_at, ingested_at)) for *rest, analyzed_at in resolved]
        return Outcome(
            anomalies=self._to_rows(resolved, counter_rows, batch, ingested_at),
            counters=list(counter_rows.values()),
            latency=_latency_rows(resolved, batch, ingested_at),
        )

    def _score(self, records: list[dict]) -> list[tuple[dict, float, bool, bool, datetime]]:
        # record, score, is_anomaly, from_cache, analyzed_at
        resolved: list[tuple[dict, float, bool, bool, datetime]] = []

        # One representative per template for cache misses, and every bypassed
        # record on its own. Without the grouping the cache would only work
        # *between* batches, so 200 identical lines arriving together would cost
        # 200 inferences — which is the common case, not the corner case.
        #
        # Grouping is part of the cache, so disabling the cache disables it too:
        # "cache off" has to mean every record reaches the model, otherwise the
        # flag cannot be used to check what the cache is costing in accuracy.
        groups: dict[str, list[dict]] = {}
        individual: list[dict] = []

        for record in records:
            outcome, entry = self._cache.get(
                self.model_key,
                self.model_revision,
                record.get("template_id"),
                record.get("message", ""),
                record.get("params"),
            )
            if outcome == "hit":
                self.counters.from_cache += 1
                resolved.append((record, entry.score, entry.is_anomaly, True, _now()))
            elif outcome == "miss" and record.get("template_id") is not None and self._cache.enabled:
                groups.setdefault(record["template_id"], []).append(record)
            else:
                if outcome == "bypass":
                    # Carried through to persistence: a record that bypassed the
                    # cache is security-relevant by definition, and collapsing
                    # those into a count would throw away the attacker's address
                    # and the username they tried — which is the whole content of
                    # the finding.
                    record["_bypassed"] = True
                individual.append(record)

        representatives = [members[0] for members in groups.values()]
        for record, score, is_anomaly, analyzed_at in self._call_detector(representatives + individual):
            template_id = record.get("template_id")
            members = groups.get(template_id) if template_id else None
            if members and members[0] is record:
                self._cache.put(self.model_key, self.model_revision, template_id, score, is_anomaly)
                # The representative's score stands for the whole group. The
                # duplicates it covered never cost an inference, so they count
                # towards the hit rate even though they missed at lookup time.
                for member in members:
                    reused = member is not record
                    if reused:
                        self.counters.from_cache += 1
                        self._cache.stats.coalesced += 1
                    resolved.append((member, score, is_anomaly, reused, analyzed_at))
            else:
                resolved.append((record, score, is_anomaly, False, analyzed_at))

        return resolved

    def _call_detector(self, records: list[dict]):
        """Batch through `detector-service`, yielding (record, score, is_anomaly, analyzed_at)."""
        for start in range(0, len(records), self._settings.batch_size):
            chunk = records[start : start + self._settings.batch_size]
            response = self._scorer(chunk)
            # Stamped per detector call, not per spool batch: a batch of 1000
            # takes many calls, and one stamp at the end would charge the last
            # call's wait to every record of the first.
            analyzed_at = _now()
            self.model_key = response["model_key"]
            self.model_revision = response["model_revision"]
            self.threshold = response["threshold"]
            self.counters.scored += len(chunk)
            for record, result in zip(chunk, response["results"], strict=True):
                yield record, result["score"], result["is_anomaly"], analyzed_at

    def _to_rows(
        self,
        resolved: list[tuple[dict, float, bool, bool, datetime]],
        counter_rows: dict,
        batch: str,
        ingested_at: datetime,
    ) -> list[dict]:
        """Turn scored records into rows, grouping repeats of one template.

        A mis-scored template is not rare and it is not cheap: one of them
        produced 4,091 findings from a single 20k-line run. Grouping happens here
        rather than at the database so the count is right even when the same
        template appears many times inside one batch.
        """
        anomalies: list[dict] = []
        grouped: dict[tuple, dict] = {}

        for record, score, is_anomaly, from_cache, analyzed_at in resolved:
            if not is_anomaly:
                continue
            self.counters.anomalies += 1
            key = (record.get("template_id") or "-", record.get("host") or "-")
            if key in counter_rows:
                counter_rows[key]["anomaly_count"] += 1

            row = {
                "host": record.get("host"),
                "agent_id": record.get("agent_id"),
                "source": record.get("source"),
                "log_timestamp": record.get("log_timestamp"),
                "template_id": record.get("template_id"),
                "template": record.get("template"),
                "message": record["message"],
                "score": score,
                "threshold": self.threshold,
                "model_key": self.model_key,
                "model_revision": self.model_revision,
                "from_cache": from_cache,
                "occurrences": 1,
                "collapsed": True,
                "anomaly_id": uuid.uuid5(ANOMALY_NAMESPACE, f"{batch}/{record['_position']}"),
                "ingested_at": ingested_at,
                "analyzed_at": analyzed_at,
            }

            if record.pop("_bypassed", False) or record.get("template_id") is None:
                # Kept whole: either the content is security-relevant, or Drain3
                # never clustered it and there is no template to group it under.
                row["collapsed"] = False
                anomalies.append(row)
                continue

            existing = grouped.get(key)
            if existing is None:
                grouped[key] = row
                anomalies.append(row)
            else:
                existing["occurrences"] += 1
                # The exemplar stays the first line seen; only the peak moves.
                existing["score"] = max(existing["score"], score)

        self.counters.anomaly_rows += len(anomalies)
        return anomalies


def _latency_rows(resolved: list[tuple], batch: str, ingested_at: datetime) -> list[dict]:
    """One sample per scored record, grouped by the minute its analysis ended.

    Every record that got a score counts, flagged or not and from the model or
    the cache: the stage being measured is the analysis, not the anomalies. Shed
    and unparseable records never reach `resolved`, so they add no sample.
    """
    rows: dict[datetime, dict] = {}
    for *_, analyzed_at in resolved:
        minute = analyzed_at.replace(second=0, microsecond=0)
        row = rows.setdefault(
            minute,
            {"minute": minute, "batch": batch, "count": 0, "sum_ms": 0.0, "max_ms": 0.0,
             "bucket_counts": [0] * BUCKETS},
        )
        elapsed_ms = (analyzed_at - ingested_at).total_seconds() * 1000
        row["count"] += 1
        row["sum_ms"] += elapsed_ms
        row["max_ms"] = max(row["max_ms"], elapsed_ms)
        row["bucket_counts"][bucket_of(elapsed_ms)] += 1
    return list(rows.values())
