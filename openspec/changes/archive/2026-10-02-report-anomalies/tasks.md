## 1. Preparation

- [x] 1.1 Create branch `feature/report-anomalies` from `feature/report-pipeline-latency` (PR #5), and rebase on `main` once PR #5 is merged.
  > Created from `docs/propose-report-anomalies` (PR #8), which sits on `feature/report-pipeline-latency` and carries this change's artifacts. The rebase on `main` is still pending until PR #5 is merged.
- [x] 1.2 Confirm the payload, limits and per-item outcomes against AnomalyWatch's `add-alert-classification` change (spec `alert-classification`, PR IvanVijandi/Anomaly-Watch#152). Any disagreement is resolved there first.
  > Matches `ReportAnomaliesRequest`/`ReportAnomaliesResponse` and `ReportAnomaliesUseCase.MaxAnomaliesPerReport` (500). Both sides treat a score equal to the threshold as anomalous (`p >= threshold` in `ml/scorer.py`, `score < threshold` rejected in `Anomaly.Create`). No disagreements.

## 2. Anomaly identity

- [x] 2.1 In `replay.py`'s `flush()`, assign a UUID4 `anomaly_id` to each row with `is_anomaly`, before storing, writing or reporting it.
- [x] 2.2 `store_postgres`: add `anomaly_id UUID` to the `CREATE TABLE`, run `ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS anomaly_id UUID`, and insert it.
- [x] 2.3 Unit tests: flagged rows have distinct ids, including two identical lines; unflagged rows have none; the JSONL row and the reported anomaly carry the same id.

## 3. Reporter

- [x] 3.1 Create `tools/anomaly_reporting.py`:
  - `to_payload(row)` with the mapping of design.md §4 (camelCase keys, ISO-8601 UTC with `Z`, `templateId` as a string);
  - `AnomalyReporter(client, endpoint, token, source, instance_id, buffer_size, timeout_s, log)`.
- [x] 3.2 `flush()`:
  - groups pending anomalies by `(model_key, model_revision, threshold)`;
  - splits each group into requests of at most 500;
  - posts with the bearer token and a 5 s timeout;
  - never raises.
- [x] 3.3 Buffer and outcomes:
  - 2xx: remove the sent anomalies and count `ACCEPTED`/`DUPLICATE`/`REJECTED` per reason from `results`; collect `alertsCreated`;
  - transport error or 5xx: keep them;
  - 4xx: log the body and discard the request;
  - overflow: drop the oldest and count them.
- [x] 3.4 `summary()`: accepted, duplicates, rejected per reason, dropped, undelivered and the created alert codes.
- [x] 3.5 Unit tests with `httpx.MockTransport`:
  - success clears the buffer;
  - an outage followed by recovery delivers each anomaly exactly once;
  - an outage longer than the buffer drops the oldest and counts them;
  - a 400 is logged and not retried;
  - 1200 pending anomalies go in three requests;
  - two model revisions go in separate requests;
  - `UNKNOWN_HOST` results are counted and not resent;
  - a resend answered as duplicates counts as duplicates.

## 4. Wiring

- [x] 4.1 `replay.py`:
  - `--report-anomalies`, `--anomalies-buffer` (default 5000) and `--log-source` (default: log file name);
  - reuse `--aw-endpoint`, `PIPELINE_SERVICE_TOKEN` and the per-run instance id of `--report-metrics`.
- [x] 4.2 Extend `reporting_config_errors`: with `--report-anomalies`, a missing endpoint, token or `--host` aborts before reading any line, naming what is missing.
- [x] 4.3 Enqueue the flagged rows of each batch and flush between `/score` calls; flush once more at the end of the run.
- [x] 4.4 Add the reporter summary to the end-of-run output. Confirm the exit code does not depend on reporting.
- [x] 4.5 Unit tests: configuration errors (endpoint, token, host) and no calls when the flag is off.

## 5. Documentation

- [x] 5.1 ARCHITECTURE.md:
  - §2.2: `anomaly-detector` owns the threshold and reports flagged anomalies; deduplication and alert triggering move to the AnomalyWatch API;
  - §3.1: add the report to the demo path diagram;
  - §5.3: lifecycle "Reported → Classified → Stored";
  - §8.1: the `anomaly_id` column.
- [x] 5.2 README: how to run the demo end to end with AnomalyWatch (`--report-anomalies --report-metrics --host web-front-01.prod.internal --aw-endpoint http://localhost:8080`) and how to read the summary.

## 6. Verification

- [x] 6.1 CI smoke test (`replay.py … --assert-anomalies 1`) without reporting flags: behavior and output unchanged.
  > Done 2026-10-02 with `baseline-bert`, offline, on `HDFS_2k.log`, against the pre-change `replay.py`: same console output apart from timings (1,105 flagged, PASS, exit 0), and the same 2,000 JSONL rows apart from the new `anomaly_id` and the timing stamps. Each of the 1,105 flagged rows has its own id; no unflagged row has one.
- [x] 6.2 `--report-anomalies` against an unreachable endpoint: the run completes with the same exit code, and the summary reports undelivered anomalies.
  > `--limit 256 --assert-anomalies 1` against `http://127.0.0.1:9`: exit 0 and PASS, with `reported : 0 accepted, …, 132 undelivered`.
- [x] 6.3 `--report-anomalies --report-metrics` against a running AnomalyWatch (disposable Postgres/Redis, seeded):
  - the accepted count matches the flagged count;
  - alerts are created for the seeded rules' patterns;
  - `GET /api/v1/metrics/response-time/summary` has samples in the alert and total stages.
  > Done 2026-10-02 with AnomalyWatch `main`, migrated and seeded, and a synthetic `auth.log` (120 lines: SSH failures, `sudo` failures and normal activity) with `--host web-front-01.prod.internal`:
  > - 60 flagged and 60 accepted;
  > - `ALT-1001` "Privilege Escalation" (HIGH, the 20 `sudo` lines) and `ALT-1002` "SSH Brute Force" (CRITICAL, the 40 SSH failures);
  > - the summary has samples in `analysis` (120), `alerting` (2) and `total` (2).
- [x] 6.4 Repeat 6.3 with the API stopped mid-run and restarted within the buffer: no missing anomalies and no duplicated alerts.
  > 240-line log, `--batch-size 8`. The API process was killed 8 s into the run and restarted while the run continued.
  > - One report failed with `ConnectError` and its 3 anomalies were kept. The run ended with 121 flagged, 121 accepted, 0 undelivered and exit 0.
  > - AnomalyWatch holds 181 distinct anomalies (60 + 121) and one alert per group.
  > - The run also opened a third alert, "Anomalous log activity". It comes from two `Accepted publickey` lines that the model scored at 0.58 and 0.51, false positives unrelated to the outage.
  > - Resending 5 of those anomalies with the same ids came back as `5 duplicate(s)`, with no new anomaly or alert.
- [x] 6.5 End-to-end verification is also tracked in AnomalyWatch's `add-alert-classification` tasks, group 9.
