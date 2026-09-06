## ADDED Requirements

### Requirement: Scoring endpoint
The system SHALL expose an HTTP scoring service that accepts log records and returns anomaly scores, so that all consumers depend on a stable contract rather than on a specific model.

#### Scenario: Batch scoring request
- **WHEN** a client POSTs a batch of log records to `/score`
- **THEN** the service SHALL return one result per input record, in the same order, each containing a numeric anomaly score in `[0, 1]` and a boolean anomaly flag derived from the configured threshold

#### Scenario: Record schema accepted
- **WHEN** a client submits a record
- **THEN** the service SHALL accept the raw message plus optional template metadata (`template_id`, template string, parameters), source host, and timestamp

#### Scenario: Batch size limit enforced
- **WHEN** a client submits a batch exceeding the configured maximum size
- **THEN** the service SHALL reject the request with a client error rather than degrading latency for other callers

#### Scenario: Malformed input rejected
- **WHEN** a client submits a record missing the raw message
- **THEN** the service SHALL return a validation error identifying the offending record index, and SHALL NOT score the batch partially without indicating which records failed

### Requirement: Configurable detection threshold
The score-to-flag threshold SHALL be configuration, not a hardcoded constant, so that false-positive rates can be tuned per deployment.

#### Scenario: Threshold configured at deploy time
- **WHEN** an operator sets the detection threshold in configuration
- **THEN** the service SHALL apply that threshold when deriving the anomaly flag, and SHALL report the active threshold on its model metadata endpoint

#### Scenario: Raw score always returned
- **WHEN** any record is scored
- **THEN** the raw numeric score SHALL be returned regardless of the threshold, so that consumers can re-threshold without re-scoring

### Requirement: Model metadata endpoint
The service SHALL report which model it is currently serving.

#### Scenario: Active model reported
- **WHEN** a client requests `/model`
- **THEN** the service SHALL return the model repository ID, pinned revision SHA, active threshold, and the evaluation metrics recorded for that model

### Requirement: Health and readiness
The service SHALL distinguish being alive from being ready to serve, so that orchestration does not route traffic to a service whose model is still loading.

#### Scenario: Readiness reflects model load
- **WHEN** the service is running but the model has not finished loading into memory
- **THEN** the readiness endpoint SHALL report not-ready, and scoring requests SHALL fail fast rather than block

#### Scenario: Missing model fails loudly at startup
- **WHEN** the configured model is absent from the local cache and Hub access is disabled
- **THEN** the service SHALL fail at startup with an error naming the missing repository and revision, rather than starting in a degraded state

### Requirement: CPU-only inference
The service SHALL run inference on CPU without requiring GPU hardware at the deployment site.

#### Scenario: Runs without GPU
- **WHEN** the service starts on a host with no GPU
- **THEN** it SHALL load the model and serve scoring requests successfully

#### Scenario: Thread usage bounded
- **WHEN** the service runs alongside the rest of the stack on a single PyME host
- **THEN** inference thread count SHALL be configurable so that the detector cannot starve co-located services

### Requirement: Containerized deployment
The service SHALL be deployable as part of the existing Docker Compose stack, consistent with the self-hosted deployment model.

#### Scenario: Compose service defined
- **WHEN** an operator brings up the stack
- **THEN** the detector service SHALL start as an internal-only service on the shared Docker network, not exposed to the host

#### Scenario: Weights mounted from a volume
- **WHEN** the container starts
- **THEN** it SHALL read model weights from a mounted cache volume, so that image size stays independent of model size and models can be swapped without rebuilding the image

### Requirement: Replay harness
The project SHALL provide a command-line harness that drives the scoring service from a log file, so the detector can be exercised without a message broker.

#### Scenario: Scoring a log file
- **WHEN** an operator runs the harness against a log file
- **THEN** it SHALL parse lines with Drain3, submit them in batches to the scoring service, and emit scored records with a summary of throughput and observed latency

#### Scenario: Harness doubles as a smoke test
- **WHEN** the harness is run against a small bundled sample file
- **THEN** it SHALL complete without external network access and report a pass/fail result usable in CI
