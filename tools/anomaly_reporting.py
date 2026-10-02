"""Anomaly reporting: sending what the detector flagged to AnomalyWatch.

The receiving contract (payload, limits, per-item outcomes) is owned by the
AnomalyWatch backend — change `add-alert-classification`, capability
`alert-classification`. This module consumes it; it does not define it.

The pipeline decides what is anomalous (the model's threshold); AnomalyWatch
decides which alert to open. So only flagged lines are reported, each with the
`anomaly_id` assigned when it was flagged: that id is AnomalyWatch's
idempotency key, which makes a resend after a timeout safe.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

# The API rejects a whole batch above this size (ReportAnomaliesUseCase.MaxAnomaliesPerReport).
MAX_ANOMALIES_PER_REQUEST = 500

DEFAULT_BUFFER_SIZE = 5000
# The API classifies synchronously, so a report takes longer than a latency report.
DEFAULT_TIMEOUT_S = 5.0
# Rejections are data problems: a few examples in the log are enough to diagnose them.
MAX_LOGGED_REJECTIONS = 5


@dataclass(frozen=True)
class Model:
    """The model that scored an anomaly, as `/score` declared it."""

    key: str
    revision: str
    threshold: float

    def to_payload(self) -> dict:
        return {"key": self.key, "revision": self.revision, "threshold": self.threshold}


def iso_utc(instant: datetime) -> str:
    return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def to_payload(row: dict, log_source: str | None) -> dict:
    """One flagged row, in the shape AnomalyWatch expects (design.md §4)."""
    template_id = row.get("template_id")
    payload = {
        "anomalyId": row["anomaly_id"],
        "host": row.get("host"),
        "logSource": log_source,
        "message": row["message"],
        "templateId": None if template_id is None else str(template_id),
        "template": row.get("template"),
        "logTimestamp": row.get("timestamp"),
        "score": row["score"],
        "ingestedAt": iso_utc(row["ingested_at"]),
        "analyzedAt": iso_utc(row["analyzed_at"]),
    }
    return {key: value for key, value in payload.items() if value is not None}


@dataclass(frozen=True)
class _Pending:
    model: Model
    payload: dict


class AnomalyReporter:
    """Sends flagged anomalies to AnomalyWatch, keeping unconfirmed ones for the next try.

    Same guarantees as `latency.Reporter`: never raises, and the buffer is bounded
    so a long outage costs the oldest anomalies (counted), not unbounded memory.
    A separate class because the payload, the chunking and the per-item result
    are different (design.md §3).
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
        log_source: str | None = None,
    ) -> None:
        self._client = client
        self._url = f"{endpoint.rstrip('/')}/api/v1/pipeline/anomalies"
        self._headers = {"Authorization": f"Bearer {token}"}
        self._source = source
        self._instance_id = instance_id
        self._log_source = log_source
        # Keyed by anomaly_id, in arrival order: removal after a partial success is
        # by id, and overflow drops from the front.
        self._pending: OrderedDict[str, _Pending] = OrderedDict()
        self._buffer_size = buffer_size
        self._timeout_s = timeout_s
        self._log = log
        self.accepted = 0
        self.duplicates = 0
        self.rejected: Counter[str] = Counter()
        self.dropped = 0
        self.discarded = 0
        self.alerts_created: list[str] = []
        self._logged_rejections = 0

    @property
    def pending(self) -> int:
        return len(self._pending)

    def enqueue(self, rows: list[dict], model: Model) -> None:
        for row in rows:
            self._pending[row["anomaly_id"]] = _Pending(model, to_payload(row, self._log_source))
        overflow = len(self._pending) - self._buffer_size
        if overflow > 0:
            for _ in range(overflow):
                self._pending.popitem(last=False)
            self.dropped += overflow
            self._log(f"anomalies: buffer full, dropped {overflow} oldest anomaly(ies) ({self.dropped} so far)")

    def flush(self) -> None:
        """Send everything pending, one request per model and at most 500 anomalies each.

        Stops at the first transport error or 5xx: the API is down, and trying the
        remaining requests would only add a timeout per request to the run.
        """
        try:
            for model, chunk in self._requests():
                if not self._send(model, chunk):
                    return
        except Exception as exc:  # noqa: BLE001 — a reporting bug must not stop detection
            self._log(f"anomalies: report failed unexpectedly ({exc.__class__.__name__}); kept for retry")

    def _requests(self) -> list[tuple[Model, list[_Pending]]]:
        groups: dict[Model, list[_Pending]] = {}
        for entry in self._pending.values():
            groups.setdefault(entry.model, []).append(entry)
        return [
            (model, entries[start : start + MAX_ANOMALIES_PER_REQUEST])
            for model, entries in groups.items()
            for start in range(0, len(entries), MAX_ANOMALIES_PER_REQUEST)
        ]

    def _send(self, model: Model, chunk: list[_Pending]) -> bool:
        """Post one request. False when the API is unreachable and the flush should stop."""
        payload = {
            "source": self._source,
            "instanceId": self._instance_id,
            "model": model.to_payload(),
            "anomalies": [entry.payload for entry in chunk],
        }

        try:
            response = self._client.post(self._url, json=payload, headers=self._headers, timeout=self._timeout_s)
        except Exception as exc:  # noqa: BLE001 — any transport failure means "retry later"
            self._log(f"anomalies: report failed ({exc.__class__.__name__}); {self.pending} anomaly(ies) kept for retry")
            return False

        if response.is_success:
            self._remove(chunk)
            self._count(response, len(chunk))
            return True

        if 400 <= response.status_code < 500:
            # The whole batch was refused; resending it unchanged would be refused again.
            self._remove(chunk)
            self.discarded += len(chunk)
            self._log(f"anomalies: report rejected ({response.status_code}): {response.text[:500]}")
            return True

        self._log(f"anomalies: report failed ({response.status_code}); {self.pending} anomaly(ies) kept for retry")
        return False

    def _remove(self, chunk: list[_Pending]) -> None:
        for entry in chunk:
            self._pending.pop(entry.payload["anomalyId"], None)

    def _count(self, response, sent: int) -> None:
        try:
            body = response.json()
            results = body["results"]
        except Exception:  # noqa: BLE001
            # Delivered, but the outcome is unknown: count it as accepted rather than resend.
            self.accepted += sent
            self._log("anomalies: report delivered but the response had no results")
            return

        for result in results:
            outcome = result.get("outcome")
            if outcome == "ACCEPTED":
                self.accepted += 1
            elif outcome == "DUPLICATE":
                self.duplicates += 1
            else:
                reason = result.get("reason") or "UNKNOWN"
                self.rejected[reason] += 1
                if self._logged_rejections < MAX_LOGGED_REJECTIONS:
                    self._logged_rejections += 1
                    detail = f": {result['detail']}" if result.get("detail") else ""
                    self._log(f"anomalies: {result.get('anomalyId')} rejected ({reason}{detail})")

        for code in body.get("alertsCreated") or []:
            if code not in self.alerts_created:
                self.alerts_created.append(code)

    def summary(self) -> str:
        rejected = sum(self.rejected.values())
        reasons = ", ".join(f"{reason} {count}" for reason, count in sorted(self.rejected.items()))
        parts = [
            f"{self.accepted} accepted",
            f"{self.duplicates} duplicate(s)",
            f"{rejected} rejected" + (f" ({reasons})" if reasons else ""),
            f"{self.dropped} dropped",
            f"{self.pending} undelivered",
        ]
        if self.discarded:
            parts.append(f"{self.discarded} discarded with their batch")
        alerts = ", ".join(self.alerts_created) if self.alerts_created else "none"
        return f"{', '.join(parts)}; alerts created: {alerts}"
