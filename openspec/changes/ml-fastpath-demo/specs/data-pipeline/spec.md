## MODIFIED Requirements

### Requirement: Anomaly detection documentation
The architecture document SHALL describe the anomaly detection stage as a fine-tuned open-weight transformer classifier served behind a dedicated scoring service, and SHALL NOT specify LogBERT trained in-house.

#### Scenario: Detection mechanism documented
- **WHEN** a contributor reads the data pipeline section
- **THEN** it SHALL describe the anomaly-detector as a client of `detector-service`, which runs a fine-tuned open-weight transformer text classifier on CPU to score log records and flag anomalies above a configurable threshold

#### Scenario: LogBERT removed as the specified mechanism
- **WHEN** a contributor reads the data pipeline or service catalog sections
- **THEN** the document SHALL NOT present in-house LogBERT training as the detection approach, and SHALL record it in the glossary only as prior art with a note that it was rejected for the demo path

#### Scenario: Model provenance documented
- **WHEN** a contributor reads the detection stage
- **THEN** it SHALL name the base model, the fine-tuning corpus, and the pinned Hugging Face revision of whichever model is currently configured as the default

### Requirement: Pipeline flow diagram
The architecture document SHALL include a Mermaid diagram showing the complete data pipeline flow, distinguishing the demo-phase transport from the target Kafka topology.

#### Scenario: Demo path shown
- **WHEN** a contributor views the data pipeline section
- **THEN** a diagram SHALL show the demo-phase flow: log file → replay harness → Drain3 parse → `detector-service` `/score` → Postgres

#### Scenario: Target topology retained
- **WHEN** a contributor views the data pipeline section
- **THEN** the Kafka-based topology SHALL remain documented as the target end state, explicitly marked as deferred to a follow-up change

## ADDED Requirements

### Requirement: Detector input contract
The parsing stage SHALL emit records carrying both the raw log message and its Drain3 template metadata, so that detection operates on natural-language text rather than template identifiers alone.

#### Scenario: Record carries raw text and template metadata
- **WHEN** the log-ingestor emits a parsed record
- **THEN** the record SHALL contain the raw message, `template_id`, the parameterized template string, extracted parameters, source host, and timestamp

#### Scenario: Detector scores raw text
- **WHEN** the detector scores a record
- **THEN** it SHALL use the raw message as the model input, with template metadata retained for grouping, classical-baseline features, and RAG context

### Requirement: Demo-phase file replay
The pipeline SHALL support replaying log files from disk through parsing and scoring without any message broker, so the model layer can be exercised before streaming infrastructure exists.

#### Scenario: Replaying a log file end to end
- **WHEN** an operator runs the replay harness against a log file
- **THEN** the harness SHALL parse each line with Drain3, submit batches to `detector-service`, and persist or print scored results with no Kafka dependency
