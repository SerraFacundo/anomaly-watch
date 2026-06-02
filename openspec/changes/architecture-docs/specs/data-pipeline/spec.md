## ADDED Requirements

### Requirement: Log collection documentation
The architecture document SHALL describe the log collection layer where FluentBit agents installed on each PyME machine collect and forward logs to the central Kafka broker.

#### Scenario: FluentBit to Kafka flow documented
- **WHEN** a contributor reads the data pipeline section
- **THEN** it SHALL describe FluentBit agents on PyME machines forwarding raw logs to Kafka topics on the central machine

### Requirement: Template extraction documentation
The architecture document SHALL describe the Drain3 log template extraction stage that consumes raw logs from Kafka and produces structured log templates.

#### Scenario: Drain3 stage documented
- **WHEN** a contributor reads the data pipeline section
- **THEN** it SHALL explain that the log-ingestor service uses Drain3 to parse raw log messages into parameterized templates (e.g., "User <*> logged in from <*>")

### Requirement: Anomaly detection documentation
The architecture document SHALL describe the LogBERT-based anomaly detection stage that processes log templates and flags anomalous patterns.

#### Scenario: LogBERT detection stage documented
- **WHEN** a contributor reads the data pipeline section
- **THEN** it SHALL describe the anomaly-detector service using LogBERT to score log template sequences and flag anomalies above a threshold

### Requirement: Pipeline flow diagram
The architecture document SHALL include a Mermaid diagram showing the complete data pipeline flow from FluentBit to alert generation.

#### Scenario: Pipeline diagram is present
- **WHEN** a contributor views the data pipeline section
- **THEN** a Mermaid diagram SHALL show the flow: FluentBit → Kafka (raw-logs topic) → log-ingestor (Drain3) → Kafka (log-templates topic) → anomaly-detector (LogBERT) → Postgres + alert trigger

### Requirement: Kafka topic structure
The architecture document SHALL document the Kafka topic structure used for inter-service communication in the pipeline.

#### Scenario: Kafka topics listed
- **WHEN** a contributor reads the data pipeline section
- **THEN** the document SHALL list at minimum two Kafka topics: one for raw logs and one for extracted templates
