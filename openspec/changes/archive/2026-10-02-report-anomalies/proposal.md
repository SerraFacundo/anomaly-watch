## Why

The pipeline detects anomalies, but AnomalyWatch never hears about them. `tools/replay.py` writes flagged lines to this stack's own Postgres (`anomalies` table), which the product does not read, so every alert in AnomalyWatch still comes from its seed data.

AnomalyWatch now has the receiving side: its change `add-alert-classification` (issue #55, "Motor de Clasificación") adds `POST /api/v1/pipeline/anomalies`, authenticated with the same service token as `--report-metrics`. It evaluates each anomaly against the detection rules an administrator configured, groups them per node and rule into alerts, and notifies. What is missing is this side: sending what the detector flagged.

It also settles a responsibility that ARCHITECTURE.md §2.2 left open. `anomaly-detector` was to own "threshold policy, deduplication and alert triggering", but grouping anomalies into alerts needs alert state (open, resolved) and the rules, and both live in AnomalyWatch. The pipeline keeps deciding *what is anomalous*; AnomalyWatch decides *which alert to open*.

## What Changes

- **Report flagged anomalies to AnomalyWatch** (`POST /api/v1/pipeline/anomalies`) behind an opt-in `--report-anomalies` flag. It reuses `--aw-endpoint` and `PIPELINE_SERVICE_TOKEN` from `--report-metrics`. Off by default: without it, `replay.py` makes no new network calls.
- **Give every flagged line an `anomaly_id`** (UUID) at scoring time. It is the idempotency key AnomalyWatch deduplicates on, so resending after a timeout never creates a second alert. It also travels in the JSONL output and the `anomalies` table.
- **Send only what the detector flagged** (`is_anomaly`). Lines below the threshold are never reported: the threshold stays the model's decision.
- **Send the provenance with each batch:** model key, revision and threshold from `/score`, plus the Drain3 template, the host, the log source and the `ingested_at`/`analyzed_at` instants that `latency-reporting` already stamps.
- **Bounded in-memory retry buffer, never blocking detection:** the same guarantees as `--report-metrics`. Unconfirmed anomalies are resent with the next report. The buffer is capped and drops the oldest anomalies, counting them. Reports are split into requests of at most 500 anomalies (the API's limit).
- **Read the per-item result:** count accepted, duplicate and rejected anomalies, and log the reasons (`UNKNOWN_HOST`, `INVALID`) aggregated, without retrying rejected items.
- **Require `--host` when reporting anomalies.** AnomalyWatch resolves each anomaly to a node of its inventory by hostname or code and rejects unknown hosts. Without `--host` every anomaly would be rejected, so the harness refuses to start instead.
- **ARCHITECTURE.md:**
  - §2.2 and §5.3 move deduplication and alert triggering to the AnomalyWatch API;
  - §3.1 adds the report to the demo path;
  - §8.1 adds `anomaly_id` to the `anomalies` table.
- `detector-service` and its `/score` contract are **unchanged**.

## Capabilities

### New Capabilities
- `anomaly-reporting`: how the pipeline identifies flagged anomalies and reports them to AnomalyWatch, with their provenance, idempotently and without affecting detection.

### Modified Capabilities
- None. `data-pipeline` still lives only inside the unarchived `architecture-docs` change, and `latency-reporting` is unchanged: this change reuses its stamps and configuration without altering its requirements.

## Impact

- **Files:**
  - `tools/replay.py`: flag, `anomaly_id`, reporting between `/score` calls, summary;
  - new `tools/anomaly_reporting.py`: payload, reporter and buffer;
  - `tests/test_anomaly_reporting.py`;
  - `ARCHITECTURE.md` and `README.md`.
- **Dependencies:** none new; `httpx` is already used.
- **Contract dependency:** the endpoint, payload, limits and per-item outcomes are owned by AnomalyWatch (`add-alert-classification`, capability `alert-classification`). This repo consumes them; it does not define them.
- **Branch dependency:** builds on `report-pipeline-latency` (PR #5), for the `Stamp` instants, `--aw-endpoint` and the token handling. It merges after it.
- **Data:** the `anomalies` table gains a nullable `anomaly_id` column (`ALTER TABLE … ADD COLUMN IF NOT EXISTS`, like the latency columns).
- **Configuration:** no new variables: `PIPELINE_SERVICE_TOKEN` and `--aw-endpoint` as today.
