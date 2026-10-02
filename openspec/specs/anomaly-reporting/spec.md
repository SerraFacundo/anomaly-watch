# anomaly-reporting Specification

## Purpose
TBD - created by archiving change report-anomalies. Update Purpose after archive.
## Requirements
### Requirement: Anomaly identity
The pipeline SHALL assign every line flagged as anomalous a unique `anomaly_id` when it is flagged. The same `anomaly_id` SHALL be used everywhere the anomaly is recorded or reported: the JSONL output, the `anomalies` table and every report to AnomalyWatch, including resends. Lines that are not flagged SHALL NOT get an `anomaly_id`.

#### Scenario: Identity shared across outputs
- **WHEN** the replay harness flags a line and writes it to the JSONL output, stores it in Postgres and reports it to AnomalyWatch
- **THEN** the three records carry the same `anomaly_id`

#### Scenario: Two identical lines
- **WHEN** a log file contains the same line twice in the same second and both are flagged
- **THEN** each occurrence gets its own `anomaly_id`

### Requirement: Opt-in anomaly reporting
The pipeline SHALL report flagged anomalies to AnomalyWatch only when anomaly reporting is explicitly enabled. It SHALL use the same AnomalyWatch endpoint and service token as latency reporting. When anomaly reporting is enabled but the endpoint, the service token or the source host is missing, the pipeline SHALL refuse to start and SHALL name what is missing. When anomaly reporting is not enabled, the pipeline SHALL make no anomaly reporting calls.

#### Scenario: Reporting disabled
- **WHEN** the replay harness runs without anomaly reporting enabled
- **THEN** it makes no calls to the anomalies endpoint and behaves as before this change

#### Scenario: Reporting enabled without a host
- **WHEN** the replay harness is started with anomaly reporting enabled and no source host
- **THEN** it exits before reading any line, with an error explaining that AnomalyWatch needs the host to resolve the node

#### Scenario: Reporting enabled without a token
- **WHEN** the replay harness is started with anomaly reporting enabled and no service token in the environment
- **THEN** it exits before reading any line, with an error naming the missing token

### Requirement: Only flagged anomalies are reported
The pipeline SHALL report only the lines the detector flagged as anomalous, with their raw score. It SHALL NOT report lines below the detector's threshold.

#### Scenario: Batch with normal and anomalous lines
- **WHEN** a batch of 64 lines is scored and 3 are flagged
- **THEN** only those 3 are reported to AnomalyWatch

### Requirement: Anomaly report content
Each report SHALL identify the source, the per-run instance identifier and the model that produced the scores: key, revision and threshold, as returned by the scoring service. Each anomaly SHALL carry its `anomaly_id`, the source host, the log source, the scored message, the Drain3 template identity and template when available, the timestamp written in the line when available, the raw score, and the `ingested_at` and `analyzed_at` instants. A single request SHALL NOT carry more than 500 anomalies, nor anomalies scored by different models.

#### Scenario: Large backlog
- **WHEN** 1200 anomalies are pending for the same model
- **THEN** the pipeline sends them in three requests of at most 500 anomalies each

#### Scenario: Model changed during a run
- **WHEN** pending anomalies were scored by two different model revisions
- **THEN** the pipeline sends one request per model revision, each declaring its own model

### Requirement: Reliable reporting without affecting detection
The pipeline SHALL report anomalies between scoring calls, never during one, and SHALL NOT let a reporting failure stop, delay beyond its request timeout or change the outcome of a detection run. Anomalies whose report fails because of a transport error or a server error SHALL be kept and resent with the next report. Kept anomalies SHALL be bounded: when the bound is exceeded, the oldest SHALL be dropped and counted. A report the API rejects as a whole with a client error SHALL be logged and discarded, not resent unchanged.

#### Scenario: AnomalyWatch unavailable mid-run
- **WHEN** AnomalyWatch stops responding during a run and comes back before the buffer fills
- **THEN** the anomalies flagged meanwhile are delivered after it comes back, the run completes, and its exit code is the same as without reporting

#### Scenario: Resend after a timeout
- **WHEN** a report times out after AnomalyWatch registered it, and the pipeline resends the same anomalies
- **THEN** AnomalyWatch answers them as duplicates and no second alert is opened

#### Scenario: Outage longer than the buffer
- **WHEN** AnomalyWatch is unavailable long enough for pending anomalies to exceed the buffer
- **THEN** the oldest pending anomalies are dropped, and the run summary reports how many

### Requirement: Per-anomaly outcome in the run summary
The pipeline SHALL read the per-anomaly result of each report and SHALL include in the end-of-run summary how many anomalies were accepted, answered as duplicates, rejected (per reason), dropped and left undelivered, and which alerts AnomalyWatch created. Rejected anomalies SHALL NOT be resent.

#### Scenario: Unknown host
- **WHEN** AnomalyWatch rejects every reported anomaly with `UNKNOWN_HOST` because the source host is not a node of its inventory
- **THEN** the summary shows those anomalies as rejected for `UNKNOWN_HOST`, and they are not resent

#### Scenario: Alerts created
- **WHEN** AnomalyWatch opens alerts from the reported anomalies
- **THEN** the summary lists the codes of the alerts it created

