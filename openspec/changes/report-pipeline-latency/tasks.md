## 1. Preparation

- [x] 1.1 Create branch `feature/report-pipeline-latency` from `main`.
- [x] 1.2 Run `uv run python tools/replay.py data/samples/HDFS_2k.log` and note the batch p50/p95 it prints today, as the baseline to compare the reported windows against in 6.3.
  > Done 2026-09-24 with `baseline-bert` on CPU (`modernbert-ft` is not on this machine). Batch 64: p50 7.6 s, p95 14.8 s. Batch 32: p50 3.4 s, p95 4.7 s. This replaced the first bucket bounds (see design, Decision 5).
- [x] 1.3 Confirm the bucket bounds and the `POST /api/v1/pipeline/latency-windows` payload against AnomalyWatch's `add-response-time-metric` change (design Decisions 4 and 7, spec `pipeline-metrics`). Any disagreement is resolved there first.

## 2. Stamping

- [x] 2.1 Add `Stamp` (`tools/latency.py`): a UTC wall-clock instant plus a monotonic reading, taken at read time and kept alongside each batched record. `ParsedRecord.to_payload()` stays unchanged, so `/score` requests carry no timing fields.
- [x] 2.2 In `replay.py`, take both readings right after reading each line and before parsing.
- [x] 2.3 In `flush()`, take `analyzed_at` and a monotonic reading when the `/score` response arrives, and compute each record's duration in milliseconds from the monotonic readings.
- [x] 2.4 Unit tests: the duration is non-negative with a patched wall clock that moves backwards, the batch wait is included, and `to_payload()` has no timing fields.

## 3. Histograms

- [x] 3.1 Create `tools/latency.py` with the bounds constant (`50 … 120000 ms, +Inf`, dense between 1 and 60 s), a `LatencyWindow` (start, count, sum, max, bucket counts) and a `WindowAggregator` that assigns samples by the UTC minute of `analyzed_at`.
- [x] 3.2 Close windows when a sample lands in a later minute, on a periodic check between batches, and at end of run.
- [x] 3.3 Unit tests: minute-boundary assignment, bucket placement at the exact bound values, the `+Inf` bucket, count == sum of bucket counts, bucket count length == bounds + 1, and a quiet minute still being closed.

## 4. Reporting

- [x] 4.1 Add `Reporter` to `tools/latency.py`: builds the payload (`source="replay"`, per-run `instance_id`, `bounds_ms`, windows), posts with a 2 s timeout and the bearer token, and removes confirmed windows from its buffer.
- [x] 4.2 Implement the bounded buffer: resend pending windows with each report, cap at `--metrics-buffer` (default 60), drop oldest first and count the drops. On a 4xx validation error, log the response body and discard that report instead of retrying it unchanged.
- [x] 4.3 Wire into `replay.py`: `--report-metrics`, `--aw-endpoint`, `--metrics-buffer`, token from `PIPELINE_SERVICE_TOKEN`. Refuse to start, naming what is missing, when reporting is enabled without endpoint or token. Report only between `/score` calls.
- [x] 4.4 Add to the end-of-run summary: windows reported, pending and dropped. Confirm the exit code does not depend on reporting outcome.
- [x] 4.5 Unit tests with `httpx.MockTransport` (no server needed): success clears the buffer, outage then recovery delivers pending windows exactly once, outage longer than the buffer drops the oldest and counts them, a 400 is logged and not retried, and missing token or endpoint aborts before reading.

## 5. Flagged anomalies

- [x] 5.1 Include `ingested_at` and `analyzed_at` in scored rows, so they reach the JSONL output (`--out`).
- [x] 5.2 In `store_postgres`, add both columns to the `CREATE TABLE` and run `ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS …` for each, so existing tables are upgraded. Insert both values.
- [x] 5.3 Verify against a real Postgres: store into a table created with the old schema and confirm the columns are added, new rows have both instants, and old rows are null. Verified with a script against a disposable `postgres:16-alpine`; not automated, since this repo has no integration-test harness yet.

## 6. Verification

- [x] 6.1 Run the CI smoke test (`replay.py … --assert-anomalies 1`) without `--report-metrics` and confirm the behavior and output are unchanged.
- [x] 6.2 Run with `--report-metrics` against an unreachable endpoint and confirm the run completes, the exit code matches 6.1, and the summary reports undelivered windows.
- [x] 6.3 Run with `--report-metrics` against a running AnomalyWatch API (disposable Postgres/Redis, stub detector). Confirm the reported `count` matches the parsed record count and that p50/p95 derived from the buckets are consistent with the batch latencies noted in 1.2.
- [ ] 6.4 End-to-end against a running AnomalyWatch is tracked in AnomalyWatch's `add-response-time-metric` tasks, group 9.

## 7. Documentation

- [x] 7.1 `ARCHITECTURE.md` §3.1: add the timing stamps and the optional latency report to the demo-path diagram and text; note that `ingested_at` becomes the Kafka `LogAppendTime` of `raw-logs` in the target topology.
- [x] 7.2 `ARCHITECTURE.md` §8.1: add `ingested_at` and `analyzed_at` to the `anomalies` column table.
- [x] 7.3 `README.md`: document `--report-metrics`, `--aw-endpoint`, `--metrics-buffer` and `PIPELINE_SERVICE_TOKEN`, with the example command.

## 8. CI (pending)

- [ ] 8.1 Add a `uv run pytest` step to `.github/workflows/ci.yml`, before the detector-service steps: the unit tests need neither model weights nor a running service, so they should fail fast. `uv sync --extra detector --extra ml` already installs the `dev` group by default. Deferred on purpose: changing CI is a separate decision from this change.
