## ADDED Requirements

### Requirement: System context documentation
The architecture document SHALL include a high-level system context section describing Anomaly Watch as a self-hosted, microservices-based log anomaly detection platform for PyMEs.

#### Scenario: System context is present
- **WHEN** a contributor opens `architecture.md`
- **THEN** the document SHALL contain a project overview describing the system's purpose, deployment model (self-hosted Docker Compose), and target audience (PyMEs)

### Requirement: System architecture diagram
The architecture document SHALL include a Mermaid diagram showing all services, their interactions, and external components (FluentBit agents, browser clients).

#### Scenario: Architecture diagram is present and renderable
- **WHEN** a contributor views `architecture.md` on GitHub
- **THEN** a Mermaid diagram SHALL render showing: FluentBit agents → Kafka → log-ingestor → anomaly-detector → rag-engine, plus api → web-app, postgres, ollama, and notifier services

### Requirement: Service catalog
The architecture document SHALL list every microservice with its technology, primary responsibility, and exposed ports.

#### Scenario: All services documented
- **WHEN** a contributor reads the service catalog section
- **THEN** the following services SHALL be listed with tech and responsibility: kafka, postgres, log-ingestor, anomaly-detector, rag-engine, api, notifier, web-app, ollama

### Requirement: Deployment model documentation
The architecture document SHALL describe the Docker Compose deployment model, including which components run on the central machine vs PyME client machines.

#### Scenario: Deployment boundaries are clear
- **WHEN** a contributor reads the deployment section
- **THEN** it SHALL be clear that FluentBit runs on PyME machines (N servers) and all other services run in a single Docker Compose stack on a central machine

### Requirement: TBD items tracking
The architecture document SHALL explicitly mark undecided items as TBD with context on what needs to be decided.

#### Scenario: TBD items are identifiable
- **WHEN** a contributor searches for "TBD" in `architecture.md`
- **THEN** all undecided architectural elements SHALL be marked with "TBD" and include a brief description of the decision needed
