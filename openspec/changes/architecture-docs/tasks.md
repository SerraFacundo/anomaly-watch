## 1. Document Structure & Header

- [ ] 1.1 Replace entire `architecture.md` content with project-specific header: title, project description, deployment model summary, and date
- [ ] 1.2 Add table of contents linking to all sections

## 2. System Architecture Diagram

- [ ] 2.1 Create Mermaid C4-style system context diagram showing: PyME machines (FluentBit), central Docker Compose stack (all services), browser/mobile clients
- [ ] 2.2 Add service interaction arrows with labeled protocols (Kafka, REST, SSE, gRPC/internal)

## 3. Service Catalog

- [ ] 3.1 Document infrastructure services: Kafka (KRaft mode), Postgres (+pgvector), Nginx (reverse proxy), Ollama (LLM runtime)
- [ ] 3.2 Document pipeline services: log-ingestor (Python/Drain3), anomaly-detector (Python/LogBERT)
- [ ] 3.3 Document application services: api (Python/FastAPI, REST+SSE), rag-engine (Python, pgvector+Ollama), notifier (Python, email/webhook)
- [ ] 3.4 Document frontend: web-app (React), mobile app (TBD framework)

## 4. Data Pipeline Section

- [ ] 4.1 Create Mermaid flow diagram: FluentBit → Kafka (raw-logs) → log-ingestor/Drain3 → Kafka (log-templates) → anomaly-detector/LogBERT → alert trigger
- [ ] 4.2 Document Kafka topic structure (raw-logs, log-templates, at minimum)
- [ ] 4.3 Document Drain3 template extraction with example (raw log → parameterized template)
- [ ] 4.4 Document LogBERT anomaly detection: training flow (TBD infra), inference flow (real-time, CPU)

## 5. RAG Response Engine Section

- [ ] 5.1 Document RAG pipeline flow: anomaly context → embedding → pgvector search → top-k ATT&CK chunks → LLM → enriched alert
- [ ] 5.2 Document MITRE ATT&CK knowledge base ingestion into pgvector
- [ ] 5.3 Document enriched alert structure: anomaly description, ATT&CK technique mapping, contingency actions
- [ ] 5.4 Mark LLM model choice as TBD (Mistral/Llama/other)

## 6. Real-Time Alerts & Notifications

- [ ] 6.1 Document SSE mechanism for dashboard real-time updates
- [ ] 6.2 Document notifier service channels: email (SMTP), webhook, additional TBD
- [ ] 6.3 Document alert lifecycle: detection → store → SSE push → notification → user acknowledgment

## 7. Auth & Security

- [ ] 7.1 Document JWT authentication with bcrypt password hashing
- [ ] 7.2 Document RBAC roles table: admin, analyst, viewer with permission matrix
- [ ] 7.3 Document security considerations: TLS, data privacy (logs stay on PyME network), API auth

## 8. Infrastructure & Deployment

- [ ] 8.1 Document Docker Compose service topology with port mappings
- [ ] 8.2 Document FluentBit agent deployment on PyME client machines (separate config)
- [ ] 8.3 Document development environment: Distrobox (ubuntu-dev container)

## 9. Data Stores

- [ ] 9.1 Document Postgres schema areas: templates, anomalies, alerts, users/roles, config
- [ ] 9.2 Document pgvector usage for MITRE ATT&CK embeddings
- [ ] 9.3 Document 1-year log retention policy with table partitioning strategy

## 10. Open Decisions & Glossary

- [ ] 10.1 Create TBD items section listing all undecided elements with context
- [ ] 10.2 Create glossary with project-specific terms: Drain3, LogBERT, MITRE ATT&CK, SSE, pgvector, KRaft, FluentBit
