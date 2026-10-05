## ADDED Requirements

### Requirement: Brokerless transport
Log records SHALL travel from a monitored host to the scoring pipeline over HTTP with no message broker in the path.

#### Scenario: No broker in the deployed stack
- **WHEN** the Compose stack is running
- **THEN** it SHALL contain no message broker service, and the only network hop between an agent and the pipeline SHALL be an HTTP request to `ingest-service`

#### Scenario: Broker remains insertable
- **WHEN** a future change requires fan-out or replay
- **THEN** the ingest contract SHALL allow a broker to be introduced behind `ingest-service` without any change to deployed agent configuration

### Requirement: Versioned wire schema
Every log record crossing the network SHALL carry an explicit schema version, and the ingest endpoint SHALL accept records from agents running older minor versions.

#### Scenario: Record envelope
- **WHEN** an agent emits a log record
- **THEN** the record SHALL contain `schema_version`, `host`, `source`, `log_timestamp`, `level`, `message`, and `agent_ts`

#### Scenario: Unknown major version rejected
- **WHEN** `ingest-service` receives a record whose `schema_version` major component it does not recognise
- **THEN** it SHALL reject the batch with a client error naming the received and supported versions

#### Scenario: Unknown fields tolerated
- **WHEN** a record within a supported major version carries fields the service does not recognise
- **THEN** the service SHALL accept the record and ignore the unknown fields

### Requirement: Agent-side durable buffering
The agent SHALL buffer records on the monitored host's disk so that central stack downtime does not lose records, and SHALL bound that buffer so it cannot exhaust the host disk.

#### Scenario: Central stack unreachable
- **WHEN** `ingest-service` is unreachable
- **THEN** the agent SHALL retain records in its filesystem buffer and retry with backoff, without dropping records while the buffer has capacity

#### Scenario: Buffer limit reached
- **WHEN** the on-disk buffer reaches its configured size limit
- **THEN** the agent SHALL stop growing the buffer rather than consume unbounded host disk

#### Scenario: Recovery after downtime
- **WHEN** `ingest-service` becomes reachable again
- **THEN** the agent SHALL forward its buffered records

### Requirement: Authenticated and encrypted ingest
The ingest endpoint SHALL require per-agent authentication and SHALL be served over TLS.

#### Scenario: Unauthenticated request rejected
- **WHEN** a request arrives at `POST /ingest` without a valid agent credential
- **THEN** the service SHALL reject it with an authentication error and SHALL NOT process any record in the batch

#### Scenario: Log data encrypted in transit
- **WHEN** an agent ships records to the central stack
- **THEN** the connection SHALL be TLS-encrypted, so log contents are not readable on the customer network

### Requirement: Batched ingest contract
`ingest-service` SHALL accept batches of records and SHALL acknowledge a batch only once it has been durably handed off to the pipeline.

#### Scenario: Batch accepted
- **WHEN** a valid authenticated batch is received
- **THEN** the service SHALL respond with success only after the batch has been handed off, so an acknowledged batch is never silently lost

#### Scenario: Partially invalid batch
- **WHEN** a batch contains records that fail validation
- **THEN** the response SHALL identify the offending record positions rather than rejecting the batch without detail

### Requirement: Backpressure signalling
When the pipeline cannot keep pace, `ingest-service` SHALL signal backpressure to agents rather than accepting records it cannot process.

#### Scenario: Pipeline saturated
- **WHEN** the collector's queue depth exceeds its configured backpressure threshold
- **THEN** `ingest-service` SHALL reject new batches with a retryable status and a retry delay

#### Scenario: Agent honours backpressure
- **WHEN** an agent receives a backpressure response
- **THEN** it SHALL retain the batch in its disk buffer and retry after the indicated delay

### Requirement: Explicit drop policy under overload
When sustained load exceeds detection capacity, the pipeline SHALL shed load in a fixed documented order and SHALL count every discarded record.

#### Scenario: Shedding order
- **WHEN** queue depth crosses successive overload thresholds
- **THEN** the pipeline SHALL first sample frequently-seen cached templates, then discard records of `INFO` and `DEBUG` severity, and only then signal backpressure

#### Scenario: High-severity records preserved
- **WHEN** the pipeline is shedding load
- **THEN** it SHALL NOT discard records of `ERROR` or `WARN` severity, nor records matching the score-cache bypass rules

#### Scenario: Discards are counted
- **WHEN** a record is discarded for any reason
- **THEN** the pipeline SHALL increment a counter tagged with the discard reason, and expose those counters on a status endpoint

#### Scenario: No silent loss
- **WHEN** an operator inspects the pipeline status
- **THEN** they SHALL be able to distinguish a period with no anomalies from a period in which records were discarded

### Requirement: Single-replica template state
The parsing stage SHALL run as exactly one replica and SHALL persist its Drain3 template state across restarts.

#### Scenario: Template identity is stable across restarts
- **WHEN** `log-collector` restarts
- **THEN** it SHALL reload its persisted Drain3 state so that a given log line keeps the `template_id` it had before the restart

#### Scenario: Replication constraint documented
- **WHEN** a contributor reads the deployment configuration
- **THEN** the single-replica requirement SHALL be stated together with its reason, namely that Drain3 learns templates online and divergent replicas produce divergent template identities
