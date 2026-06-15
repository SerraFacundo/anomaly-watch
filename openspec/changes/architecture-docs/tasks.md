## 1. Document Structure & Header

- [x] 1.1 Replace entire `architecture.md` content with project-specific header: title, project description, deployment model summary, and date
- [x] 1.2 Add table of contents linking to all sections

## 2. System Architecture Diagram

- [x] 2.1 Create Mermaid C4-style system context diagram showing: PyME machines (FluentBit), central Docker Compose stack (all services), browser/mobile clients
- [x] 2.2 Add service interaction arrows with labeled protocols (Kafka, REST, SSE, gRPC/internal)

## 3. Service Catalog

- [x] 3.1 Document infrastructure services: Kafka (KRaft mode), Postgres (+pgvector), Nginx (reverse proxy), Ollama (LLM runtime)
- [x] 3.2 Document pipeline services: log-ingestor (Python/Drain3), anomaly-detector (Python/LogBERT)
- [x] 3.3 Document application services: api (Python/FastAPI, REST+SSE), rag-engine (Python, pgvector+Ollama), notifier (Python, email/webhook)
- [x] 3.4 Document frontend: web-app (React), mobile app (TBD framework)

## 4. Data Pipeline Section

- [x] 4.1 Create Mermaid flow diagram: FluentBit → Kafka (raw-logs) → log-ingestor/Drain3 → Kafka (log-templates) → anomaly-detector/LogBERT → alert trigger
- [x] 4.2 Document Kafka topic structure (raw-logs, log-templates, at minimum)
- [x] 4.3 Document Drain3 template extraction with example (raw log → parameterized template)
- [x] 4.4 Document LogBERT anomaly detection: training flow (TBD infra), inference flow (real-time, CPU)

## 5. RAG Response Engine Section

- [x] 5.1 Document RAG pipeline flow: anomaly context → embedding → pgvector search → top-k ATT&CK chunks → LLM → enriched alert
- [x] 5.2 Document MITRE ATT&CK knowledge base ingestion into pgvector
- [x] 5.3 Document enriched alert structure: anomaly description, ATT&CK technique mapping, contingency actions
- [x] 5.4 Mark LLM model choice as TBD (Mistral/Llama/other)

## 6. Real-Time Alerts & Notifications

- [x] 6.1 Document SSE mechanism for dashboard real-time updates
- [x] 6.2 Document notifier service channels: email (SMTP), webhook, additional TBD
- [x] 6.3 Document alert lifecycle: detection → store → SSE push → notification → user acknowledgment

## 7. Auth & Security

- [x] 7.1 Document JWT authentication with bcrypt password hashing
- [x] 7.2 Document RBAC roles table: admin, analyst, viewer with permission matrix
- [x] 7.3 Document security considerations: TLS, data privacy (logs stay on PyME network), API auth

## 8. Infrastructure & Deployment

- [x] 8.1 Document Docker Compose service topology with port mappings
- [x] 8.2 Document FluentBit agent deployment on PyME client machines (separate config)
- [x] 8.3 Document development environment: Distrobox (ubuntu-dev container)

## 9. Data Stores

- [x] 9.1 Document Postgres schema areas: templates, anomalies, alerts, users/roles, config
- [x] 9.2 Document pgvector usage for MITRE ATT&CK embeddings
- [x] 9.3 Document 1-year log retention policy with table partitioning strategy

## 10. Open Decisions & Glossary

- [x] 10.1 Create TBD items section listing all undecided elements with context
- [x] 10.2 Create glossary with project-specific terms: Drain3, LogBERT, MITRE ATT&CK, SSE, pgvector, KRaft, FluentBit
