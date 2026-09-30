## Context

**What exists in this repo** (on top of `report-pipeline-latency`, PR #5):
- **`replay.py`** reads a log file and parses each line with Drain3 (`message`, `template_id`, `template`, `params`, `host` from `--host`, `timestamp`). It scores in batches against `detector-service` (`POST /score` → `score`, `is_anomaly`, `threshold`, `model_key`, `model_revision`) and keeps the rows with `is_anomaly`.
- **Each scored row** carries `ingested_at` and `analyzed_at` (UTC, `latency-reporting`).
- **`--report-metrics`** reports latency windows to AnomalyWatch with a `Reporter` that has a bounded buffer, never raises, and discards a report the API rejects with a 4xx. The token comes from `PIPELINE_SERVICE_TOKEN`, and a missing endpoint or token aborts before reading any line.
- **`--store-postgres`** writes flagged rows to the `anomalies` table of this stack's Postgres.

**What exists in AnomalyWatch** (change `add-alert-classification`, PR IvanVijandi/Anomaly-Watch#152, capability `alert-classification`):
- **Endpoint:** `POST /api/v1/pipeline/anomalies` with `{source, instanceId, model: {key, revision, threshold}, anomalies: [...]}`. Each anomaly carries `anomalyId`, `host`, `logSource?`, `message`, `templateId?`, `template?`, `logTimestamp?`, `score`, `ingestedAt` and `analyzedAt`.
- **Validation:**
  - a batch with no model, empty or over 500 anomalies is a 400 for the whole batch;
  - each anomaly is answered `ACCEPTED`, `DUPLICATE` (its `anomalyId` was already registered) or `REJECTED` with `UNKNOWN_HOST` or `INVALID` (score out of range or below the threshold, empty message or over 8 KB, `analyzedAt < ingestedAt`).
- **Classification:** the API resolves `host` to a node by hostname or code, evaluates the rules, groups anomalies into alerts and notifies `ALERT_CREATED`.

## Goals / Non-Goals

**Goals:**
- Every line the detector flags reaches AnomalyWatch when reporting is enabled, exactly once from AnomalyWatch's point of view, even across timeouts and API restarts within the buffer.
- Reporting never slows down, blocks or fails a detection run, and never changes its exit code.
- The run summary says how many anomalies were accepted, duplicated, rejected (and why), dropped or left undelivered.

**Non-Goals:**
- Classifying, grouping or deduplicating into alerts: AnomalyWatch does it.
- Reporting lines below the threshold, or reporting all logs (log ingestion is a separate change).
- Persisting the retry buffer across runs: a run that ends with undelivered anomalies reports them in the summary. The `anomalies` table and the JSONL output still have them, with their `anomaly_id`, for a manual resend.
- Unifying `Reporter` and the new reporter into one class while PR #5 is open (Open Questions).

## Decisions

### 1. The pipeline decides what is anomalous; AnomalyWatch decides which alert to open

This mirrors AnomalyWatch's Decision 1 and its ADR_0030. The detector's threshold is a property of the model (`DETECTOR_THRESHOLD`, versioned with it), so only flagged lines are sent. Grouping into alerts needs alert state and the detection rules, which live in AnomalyWatch.

ARCHITECTURE.md §2.2 changes accordingly. `anomaly-detector` owns the threshold policy and *reports* flagged anomalies; alert triggering and deduplication move to the AnomalyWatch API. §5.3's "Detected → Stored" becomes "Reported → Classified → Stored".

**Rejected alternative: the pipeline opens alerts already classified.** It would duplicate the rules in Python and require querying AnomalyWatch for open alerts before each decision.

### 2. `anomaly_id` is assigned once, when the line is flagged

A UUID4 is generated in `flush()` for each row with `is_anomaly`, before the row is stored, written to JSONL or reported. The same id travels to all three, so:
- a resend after a timeout is answered `DUPLICATE` instead of opening a second alert;
- a row in `anomalies` can be matched against AnomalyWatch's evidence (`GET /api/v1/alerts/{id}/evidence` returns the same id).

Deriving the id from the content (hash of message + timestamp) was rejected: two identical lines in the same second are two occurrences, and they matter for rules with an occurrence threshold.

### 3. A dedicated reporter with the same guarantees as `Reporter`

`tools/anomaly_reporting.py` adds `AnomalyReporter`:

| Behavior | Value |
|---|---|
| When it sends | Between `/score` calls, after each batch, never during one: reporting cannot delay scoring |
| Request size | At most 500 anomalies per request (the API's limit); a larger backlog goes in several requests |
| Timeout | 5 s per request, since the API classifies synchronously |
| Buffer | Bounded (`--anomalies-buffer`, default 5000); drops the oldest first and counts them |
| Transport error or 5xx | Keeps the anomalies for the next flush |
| 2xx | Removes the sent anomalies. Counts `ACCEPTED`, `DUPLICATE` and `REJECTED` per reason from `results`, and collects `alertsCreated` |
| 4xx for the whole batch | Logs the body and discards that request: resending it unchanged would be rejected again |
| Exceptions | None escape `flush()` |

It follows `Reporter`'s shape but is a separate class, because the payload, the chunking and the per-item result are different, and PR #5 is still open (Open Questions).

The end-of-run flush retries once. What remains undelivered is reported in the summary; the exit code does not change.

### 4. Payload mapping

| API field | Source |
|---|---|
| `source` | `"replay"` |
| `instanceId` | The same per-run id `--report-metrics` uses, so both reports of a run can be correlated |
| `model.key`, `model.revision`, `model.threshold` | From the `/score` response of the batch where the line was flagged |
| `anomalyId` | Decision 2 |
| `host` | `--host` |
| `logSource` | `--log-source`, default: the log file name |
| `message` | The scored message (the same text sent to `/score`) |
| `templateId`, `template` | Drain3; `templateId` as a string |
| `logTimestamp` | The timestamp written in the line, unparsed |
| `score` | Raw score |
| `ingestedAt`, `analyzedAt` | The `latency-reporting` stamps, ISO-8601 UTC with `Z` |

**Batches with different models.** Rows reported together can come from `/score` responses with different model keys (a detector restart with another model mid-run is possible). `flush()` groups pending anomalies by `(key, revision, threshold)` and sends one request per group.

### 5. `--host` is required with `--report-anomalies`

AnomalyWatch rejects anomalies whose host is not a node of its inventory, and it does not create nodes. Replaying a sample file without `--host` would produce only `UNKNOWN_HOST`. So `--report-anomalies` without `--host` refuses to start and says so, like a missing token.

Nodes are registered in AnomalyWatch (seeded in the demo); the README gives the seeded hostname to use, `web-front-01.prod.internal`.

### 6. Rejections are counted, not retried

`UNKNOWN_HOST` and `INVALID` are properties of the data, so resending would give the same answer. They are counted per reason, and the first few are logged with the anomaly id. A burst of `UNKNOWN_HOST` usually means a wrong `--host`, and the summary makes that visible.

## Risks / Trade-offs

- **[Two reporters with the same buffering logic]** → Accepted while PR #5 is open. Unifying them into a generic buffered poster is a small follow-up (Open Questions).
- **[Synchronous classification makes a report slower than a latency report]** → Reports happen between batches and a batch takes seconds on CPU, so a 5 s timeout is enough. On timeout, the anomalies stay pending and are resent as `DUPLICATE`-safe.
- **[Buffer overflow during a long outage]** → Oldest anomalies are dropped and counted. They remain in `--out` and `--store-postgres`, with their `anomaly_id`, for a manual resend.
- **[Wrong `--host` silently producing no alerts]** → Every anomaly comes back `UNKNOWN_HOST`, and the summary prints it.

## Migration Plan

1. Merge after `report-pipeline-latency` (PR #5).
2. The `anomalies` table gains `anomaly_id` via `ADD COLUMN IF NOT EXISTS`; existing rows keep it null.
3. Without `--report-anomalies`, nothing changes. Rollback: revert the change; AnomalyWatch simply stops receiving anomalies.

## Open Questions

- **One reporter for both reports:** once PR #5 is merged, should `Reporter` and `AnomalyReporter` share a generic buffered poster?
- **Persisted retry queue:** when the streaming topology replaces `replay.py`, should undelivered anomalies survive a restart (Kafka topic or local spool)? It belongs to the streaming change, not the demo path.
- **Resend tool:** is a small `tools/resend_anomalies.py` (from the `anomalies` table, by `anomaly_id`) worth it for the demo, or is re-running `replay.py` enough?
