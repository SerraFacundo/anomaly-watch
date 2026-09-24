## ADDED Requirements

### Requirement: Ingestion instant
The pipeline SHALL stamp every record with `ingested_at`, a UTC instant taken when the pipeline receives the log line. `ingested_at` SHALL NOT be derived from the timestamp written in the log line; that timestamp SHALL continue to travel unchanged as the record's `timestamp`.

#### Scenario: Replaying a historical dataset
- **WHEN** the replay harness processes a log file whose lines carry timestamps from years ago
- **THEN** each record's `ingested_at` is the moment the harness read the line, and its `timestamp` still holds the value written in the line

#### Scenario: Line without a recognizable timestamp
- **WHEN** the replay harness reads a line whose timestamp cannot be parsed
- **THEN** the record still gets an `ingested_at`, and its `timestamp` is absent as today

### Requirement: Analysis instant and duration
The pipeline SHALL stamp every scored record with `analyzed_at`, a UTC instant taken when the scoring response for that record's batch is received. The pipeline SHALL measure each record's analysis duration as the time elapsed between receiving the line and receiving that response, using a monotonic clock in the same process. The duration SHALL include parsing, the wait for the batch to fill, and the scoring round trip.

#### Scenario: Wall clock adjusted during a run
- **WHEN** the system's wall clock is moved backwards while records are waiting for their batch to be scored
- **THEN** the analysis durations of those records are non-negative and reflect the real elapsed time

#### Scenario: Batch wait is part of the duration
- **WHEN** a record is the first of a batch and the batch is scored after the remaining records are read
- **THEN** that record's analysis duration includes the time spent waiting for the batch to fill

### Requirement: Scoring contract unchanged
The pipeline SHALL NOT send `ingested_at`, `analyzed_at` or any timing data to `detector-service`. The records sent for scoring SHALL contain only the fields the scoring contract declares.

#### Scenario: Scoring request content
- **WHEN** the replay harness sends a batch for scoring with latency reporting enabled
- **THEN** each record in the request contains only the declared record fields, with no timing fields

### Requirement: Per-minute analysis histograms
The pipeline SHALL aggregate analysis durations into one window per UTC minute, assigning each sample to the minute of its `analyzed_at`. Each window SHALL hold the sample count, the sum of durations in milliseconds, the maximum duration in milliseconds, and the count of samples per bucket, over the bucket bounds published by AnomalyWatch's `pipeline-metrics` capability. A window SHALL be closed when its minute has passed, including when no further samples arrive, and at the end of a run.

#### Scenario: Samples across a minute boundary
- **WHEN** records are analyzed during 14:05 and 14:06 UTC
- **THEN** the pipeline produces one window starting at 14:05:00 and one starting at 14:06:00, each counting only the samples analyzed within its minute

#### Scenario: Window consistency
- **WHEN** a window is closed
- **THEN** the sum of its bucket counts equals its sample count, and it has exactly one more bucket count than there are bounds

#### Scenario: Quiet minute after activity
- **WHEN** samples arrive during a minute and none arrive in the following minutes
- **THEN** the window for the active minute is closed and reported without waiting for another sample

### Requirement: Opt-in reporting to AnomalyWatch
The pipeline SHALL report closed windows to AnomalyWatch only when latency reporting is explicitly enabled. When enabled, each report SHALL identify the source, an instance identifier unique to the running process, and the bucket bounds in use, and SHALL authenticate with the service token taken from the environment. When reporting is enabled but the AnomalyWatch endpoint or the service token is missing, the pipeline SHALL refuse to start and SHALL name what is missing. When reporting is not enabled, the pipeline SHALL make no reporting calls.

#### Scenario: Reporting disabled
- **WHEN** the replay harness runs without latency reporting enabled
- **THEN** it makes no calls to AnomalyWatch and behaves as before this change

#### Scenario: Reporting enabled with complete configuration
- **WHEN** the replay harness runs with latency reporting enabled, an AnomalyWatch endpoint and a service token
- **THEN** it reports each closed window to AnomalyWatch with its source, instance identifier and bucket bounds

#### Scenario: Reporting enabled without a token
- **WHEN** the replay harness is started with latency reporting enabled and no service token in the environment
- **THEN** it exits before reading any line, with an error naming the missing token

### Requirement: Bounded retry of unconfirmed windows
The pipeline SHALL keep windows that AnomalyWatch has not confirmed in an in-memory buffer and SHALL resend them with the next report. Confirmed windows SHALL leave the buffer. The buffer SHALL have a configurable capacity; when full, the pipeline SHALL drop the oldest windows first and SHALL count them. The number of dropped windows SHALL be logged when it happens and included in the end-of-run summary.

#### Scenario: Temporary outage
- **WHEN** AnomalyWatch is unreachable for three minutes and then recovers, within the buffer's capacity
- **THEN** the pipeline delivers the three pending windows with its next report after recovery, and no window is lost or duplicated

#### Scenario: Outage longer than the buffer
- **WHEN** AnomalyWatch stays unreachable for longer than the buffer's capacity in windows
- **THEN** the pipeline drops the oldest windows, keeps the most recent ones up to the capacity, and reports how many it dropped

### Requirement: Reporting never affects detection
Reporting SHALL NOT block, delay beyond a short bounded timeout, or fail detection. A reporting call SHALL NOT run while a scoring request is in flight. A failed, rejected or timed-out report SHALL NOT stop the run and SHALL NOT change the pipeline's exit code.

#### Scenario: AnomalyWatch down for the whole run
- **WHEN** the replay harness runs with reporting enabled and AnomalyWatch never responds
- **THEN** every line is parsed and scored, results are written as requested, the exit code is the same as with reporting disabled, and the summary reports the undelivered and dropped windows

#### Scenario: Report rejected by AnomalyWatch
- **WHEN** AnomalyWatch rejects a report with a validation error
- **THEN** the pipeline logs the error returned, keeps scoring, and does not retry that report unchanged

### Requirement: Flagged anomalies carry their timing
Every anomaly the pipeline flags SHALL carry its `ingested_at` and `analyzed_at` in every output it is written to, including the JSONL output and the `anomalies` table. Anomalies stored before this change SHALL remain readable, with both instants absent.

#### Scenario: Anomaly written to Postgres
- **WHEN** the replay harness stores flagged anomalies in Postgres
- **THEN** each stored row has its `ingested_at` and `analyzed_at`

#### Scenario: Existing anomalies table
- **WHEN** the replay harness stores anomalies in a database whose `anomalies` table was created before this change
- **THEN** the table gains the two columns, the new rows are stored with both instants, and the existing rows keep them empty
