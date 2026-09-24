## Context

AnomalyWatch's `add-response-time-metric` change defines the response-time metric over three instants per log — `ingestedAt`, `analyzedAt`, `alertedAt` — and three stages: `ANALYSIS` (every log), `ALERTING` and `TOTAL` (only logs that raise an alert). The backend owns the API contract, storage and percentile math. It receives analysis latency already aggregated, so it cannot observe how it was measured. That part is this change.

Demo path today: `replay.py` reads a file line by line, parses with Drain3 (`tools/parser.py`), accumulates batches of 64, posts them to `detector-service`, and optionally writes flagged anomalies to Postgres. There is no broker; Kafka remains the target topology (ARCHITECTURE §3.1).

The whole stack runs on a single PyME host, so the pipeline, the detector and the AnomalyWatch API share one wall clock.

## Goals / Non-Goals

**Goals:**
- An `ingested_at` that means "the platform received this line", and an analysis duration that is immune to wall-clock adjustments.
- Mergeable per-minute aggregates that match AnomalyWatch's bucket bounds exactly.
- Reporting that tolerates the API being down for a while, and that can never slow down or fail detection.
- Timing carried on flagged anomalies so the future alert-creation call can forward it.

**Non-Goals:**
- Creating alerts in AnomalyWatch. The `ALERTING` and `TOTAL` stages stay empty until that exists.
- Kafka, `log-ingestor` or `anomaly-detector`. The design notes how the stamps map onto them, but nothing is built.
- Distributed tracing or OpenTelemetry instrumentation (AnomalyWatch's design, Decision 1, explains why the metric is domain data rather than telemetry).
- Latency measured from when the event happened on the source machine. That is a different metric, subject to source clock skew.

## Decisions

### 1. `ingested_at` is stamped on read, never parsed from the line

`ingested_at` is taken when the pipeline receives the line: in `replay.py`, right after reading it and before parsing. The timestamp written in the line stays in `timestamp`, unchanged, for display and RAG context.

In the target topology, `ingested_at` becomes the Kafka record timestamp of `raw-logs` with `message.timestamp.type=LogAppendTime` — the broker's append time, stamped when the log reaches the platform, at no extra cost.

**Rejected — parse `ingested_at` from the log line**: it is free-form text in each source's format and uses each source machine's clock. Replaying a historical dataset would turn it into a measure of the dataset's age.

### 2. The duration is measured monotonically; wall-clock instants are only for reporting

Each record keeps two readings taken at the same moment: `ingested_at` (UTC wall clock, for the record and the future alert) and a `time.perf_counter()` value. When the batch response arrives, the pipeline takes both readings again. The analysis duration is the difference between the monotonic readings. A wall-clock adjustment mid-run (NTP step, manual change) cannot make it negative or inflate it.

The monotonic reading is internal: it is not serialized, sent to the detector or stored.

### 3. Analysis includes batch wait, on purpose

The duration covers parse + waiting for the batch to fill + the `/score` round trip. The first record of a 64-record batch pays for the other 63 being read. That is real latency a PyME experiences, and it is exactly the input ARCHITECTURE §9 ("Streaming topology") says should size the Kafka design. The detector's own `elapsed_ms` (pure inference) is not reported as a separate stage in this change; AnomalyWatch lists that as an open question.

### 4. Timing stays out of the `/score` contract

`ParsedRecord.to_payload()` keeps producing only the fields the detector declares. Timing lives in a pipeline-side wrapper around each record. `detector-service`'s schema documents itself as "the stable surface every downstream consumer depends on", and the detector has no use for these fields. Pydantic would silently ignore them today, but relying on that would couple the contract to a default.

### 5. Per-minute histograms with AnomalyWatch's bounds

Each sample is assigned to the UTC minute of its `analyzed_at` (when the measurement completes). A window holds `count`, `sum_ms`, `max_ms` and `bucket_counts` over the bounds published by AnomalyWatch:

`10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000, +Inf` (ms)

The bounds are declared once in `tools/latency.py` and sent with every report. AnomalyWatch rejects mismatched bounds with a 400, which is the safety net if they drift — not the synchronization mechanism. A change to the bounds starts on the AnomalyWatch side.

A window is closed when a sample lands in a later minute, or when a periodic check sees that its minute has passed. A quiet minute therefore still gets flushed. At end of run, the open window is closed and reported as-is.

### 6. Reporting: opt-in, bounded buffer, never blocking

- Enabled only with `--report-metrics`. It requires `--aw-endpoint` and `PIPELINE_SERVICE_TOKEN` in the environment. If either is missing, the run refuses to start, naming what is missing. This follows the detector's rule that a half-configured component should fail loudly.
- Each report sends the pending windows plus the one just closed, with `source="replay"` and an `instance_id` generated once per run. AnomalyWatch replaces a window by `(source, instance_id, stage, window_start)` instead of adding to it, so resending is safe.
- Windows the API confirms leave the buffer. On timeout or error, they stay and are retried with the next report.
- The buffer is capped (default 60 windows, `--metrics-buffer`). When full, the oldest windows are dropped and counted. The count is logged and printed in the end-of-run summary.
- Requests use a short timeout (2 s) and happen at batch boundaries, between `/score` calls, never inside one. Reporting failures never change the exit code: `--assert-anomalies` keeps its current meaning.
- The buffer does not survive a process restart. Persisting it would be a durable queue for data that tolerates loss.

**Rejected — a background thread for reporting**: `replay.py` is single-threaded, and reporting between batches keeps it that way. A 2 s timeout once a minute is negligible next to batch scoring. `anomaly-detector` can move it to a thread or task when it exists.

### 7. Flagged anomalies carry both instants

The JSONL output and the `anomalies` table gain `ingested_at` and `analyzed_at`. `store_postgres` currently runs `CREATE TABLE IF NOT EXISTS`, which does not add columns to an existing table, so it also runs `ALTER TABLE … ADD COLUMN IF NOT EXISTS` for both. Existing rows keep them null.

## Risks / Trade-offs

- **Bounds drift between repos** → A single constant here, a 400 from AnomalyWatch on mismatch, and a task in both changes to update the other side together.
- **Clock skew if the pipeline runs on a different host from the API** → Does not affect `ANALYSIS`, which is measured monotonically in one process. It only affects the future `ALERTING` stage, and AnomalyWatch excludes alerts whose instants disagree beyond a tolerance.
- **Losing windows if the API is down longer than the buffer** → Bounded and visible: dropped windows are counted and reported. Chosen over an unbounded buffer, which would turn a long outage into a memory problem on a PyME host.
- **Replay runs are bursty** → A 2k-line file finishes in seconds, so a run produces one or two windows. Correct but thin. Larger samples or `--limit` runs give a better picture, and the AnomalyWatch summary always shows `count`.

## Migration Plan

1. Implement against the contract in AnomalyWatch `add-response-time-metric` (can proceed in parallel with the backend).
2. Verify locally without AnomalyWatch using a stub server (see tasks).
3. End-to-end verification is owned by AnomalyWatch's tasks (group 9) and needs both changes.
4. Rollback: the flag is opt-in; not passing it restores today's behavior. The new `anomalies` columns are nullable.

## Open Questions

- Report the detector's `elapsed_ms` as an `INFERENCE` sub-stage? Depends on AnomalyWatch accepting that stage.
- Should `anomaly-detector`, once it exists, report per instance (one `instance_id` per consumer) or per consumer group? Per instance is what the contract assumes.
