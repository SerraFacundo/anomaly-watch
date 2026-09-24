## Why

Anomaly Watch issue #28 (KPI Cards) asks for a **response-time indicator**: how long it takes from the moment a log enters the platform until it is analyzed and, when it is anomalous, until an alert exists. The AnomalyWatch backend is adding the storage, query API and dashboard contract for that metric in its change `add-response-time-metric` (capability `pipeline-metrics`).

The backend can only store and aggregate what it is told. Two of the three instants the metric is built on — when a log was ingested and when it finished being analyzed — only exist inside this pipeline, and today nothing records them:

- `tools/replay.py` measures per-batch latency against `detector-service`, but only prints p50/p95 to the console at the end of a run.
- Records carry the timestamp written in the log line, which is the source machine's clock and says nothing about when the platform received it. Replaying `HDFS_2k.log` would report latencies of eighteen years.
- Flagged anomalies are stored without any timing information, so the future alert-creation call has nothing to forward.

This change makes the pipeline stamp those instants, measure the analysis stage, and report it to AnomalyWatch — without letting measurement ever slow down or break detection.

## What Changes

- **Stamp `ingested_at`** (UTC) when a line enters the pipeline — in the demo path, when `replay.py` reads it — never from the log's own timestamp.
- **Stamp `analyzed_at`** when the `/score` response for the record's batch arrives, and measure the analysis duration on a **monotonic clock** in the same process.
- **Aggregate analysis latency into per-minute histograms** using the bucket bounds AnomalyWatch publishes, so windows can be merged into correct percentiles for any time range.
- **Report windows to AnomalyWatch** (`POST /api/v1/pipeline/latency-windows`) with a service token, behind an opt-in `--report-metrics` flag. Off by default: without it, `replay.py` makes no new network calls.
- **Bounded in-memory retry buffer**: unconfirmed windows are resent with the next report; the buffer is capped and drops the oldest windows, counting what it dropped. Reporting never blocks or fails a detection run.
- **Flagged anomalies keep `ingested_at` and `analyzed_at`** in the JSONL output and the `anomalies` table, ready for the alert-creation call.
- `detector-service` and its `/score` contract are **unchanged**. Timing metadata stays on the pipeline side.

## Capabilities

### New Capabilities
- `latency-reporting`: how the pipeline stamps ingestion and analysis instants, measures and aggregates analysis latency, and reports it to AnomalyWatch reliably without affecting detection.

### Modified Capabilities
- None. `data-pipeline` and `anomaly-detector-service` still live only inside unarchived changes (`architecture-docs`, `ml-fastpath-demo`). A new capability avoids a delta that could only be applied after those are archived.

## Impact

- **Files**: `tools/replay.py` (stamps, flags, reporting), new `tools/latency.py` (stamps, histogram, reporter + buffer), new `tests/` with `pytest` in a `dev` dependency group, `ARCHITECTURE.md` (§3.1 demo path, §8.1 `anomalies` columns), `README.md`. `tools/parser.py` is unchanged: the stamp is taken by the caller, around it.
- **Dependencies**: none new — `httpx` is already used by `replay.py`.
- **Contract dependency**: bucket bounds and payload are owned by AnomalyWatch (`add-response-time-metric`, `pipeline-metrics`). This repo consumes them; it does not define them.
- **Systems**: `detector-service` untouched. The `anomalies` table gains two nullable columns.
- **Configuration**: `PIPELINE_SERVICE_TOKEN` environment variable, same value as the AnomalyWatch deployment.
- **CI**: pending. The new unit tests are not run by `ci.yml` yet; adding a `uv run pytest` step is tracked as task 8.1.
