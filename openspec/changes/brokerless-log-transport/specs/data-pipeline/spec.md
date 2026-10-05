## MODIFIED Requirements

### Requirement: Pipeline flow diagram
The architecture document SHALL include a Mermaid diagram showing the complete data pipeline flow, and that flow SHALL be brokerless.

#### Scenario: Target topology shown
- **WHEN** a contributor views the data pipeline section
- **THEN** a diagram SHALL show: FluentBit agent → `ingest-service` → `log-collector` (Drain3) → score cache → `detector-service` → Postgres

#### Scenario: Demo path retained
- **WHEN** a contributor views the data pipeline section
- **THEN** the file-replay path SHALL remain documented as the offline test and load-generation path, not as a deferred stand-in for a transport that does not exist

#### Scenario: Kafka removed as the target
- **WHEN** a contributor reads the data pipeline or infrastructure sections
- **THEN** Kafka SHALL NOT be presented as the target transport, and the reason SHALL be recorded, namely that measured detector throughput and PyME deployment constraints do not justify a broker

### Requirement: Transport documentation
The architecture document SHALL describe how log records travel from monitored machines to the pipeline, including the buffering guarantee and the escape hatch if a broker is later required.

#### Scenario: Agent buffering documented
- **WHEN** a contributor reads the transport description
- **THEN** it SHALL state that the agent's bounded filesystem buffer is the queue, and what happens when the central stack is unreachable

#### Scenario: Broker escape hatch recorded
- **WHEN** a contributor asks why there is no broker
- **THEN** the document SHALL record the alternatives considered, the throughput measurement that drove the decision, and the conditions under which a broker would be reintroduced

### Requirement: Log retention policy
The retention policy SHALL be sized to the storage available on PyME hardware and SHALL state what is discarded.

#### Scenario: Anomalies retained
- **WHEN** a record is flagged as an anomaly
- **THEN** it SHALL be persisted with its raw message, template metadata, raw score, threshold, model key and model revision, and retained indefinitely

#### Scenario: Aggregates retained
- **WHEN** records are processed
- **THEN** the pipeline SHALL maintain per-template hourly counters including total and anomalous counts per host, retained for one year

#### Scenario: Normal lines discarded
- **WHEN** a record is scored below the threshold and is not retained by an aggregate
- **THEN** it SHALL be discarded after scoring, and the document SHALL state plainly that normal-line forensic history is not available

#### Scenario: Retention is costed
- **WHEN** a contributor reads the retention policy
- **THEN** it SHALL state the expected row volume per year, so the policy can be checked against the hardware the product is deployed on

#### Scenario: Compliance fallback documented
- **WHEN** a deployment requires raw log retention for compliance
- **THEN** the document SHALL describe compressed on-disk archival outside Postgres as the supported fallback, and note that it is not queryable from the dashboard

### Requirement: Service catalog accuracy
The service catalog SHALL list the services that carry log traffic and SHALL distinguish implemented services from planned ones.

#### Scenario: Transport services catalogued
- **WHEN** a contributor reads the service catalog
- **THEN** `ingest-service` and `log-collector` SHALL be listed with their responsibilities and ports, and `log-ingestor` and `anomaly-detector` SHALL no longer be listed as Kafka consumers

#### Scenario: Replica constraint stated
- **WHEN** a contributor reads the `log-collector` entry
- **THEN** it SHALL state that the service runs as a single replica because Drain3 template state cannot be replicated without shared state
