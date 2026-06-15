## Why

The project has no architecture documentation reflecting actual design decisions. The current `architecture.md` is a generic template with placeholder sections. The team has made significant architectural decisions (microservices, log pipeline, ML model, RAG-based response engine) that need to be documented as a living reference for contributors and agents. Without this, every new contributor or agent must rediscover the architecture from scratch.

## What Changes

- Replace the generic template `architecture.md` with a comprehensive, project-specific architecture document reflecting all decisions made
- Document the full system topology: self-hosted Docker Compose stack deployed by PyMEs, with FluentBit agents on client machines
- Document the data pipeline: FluentBit → Kafka → Drain3 → LogBERT → anomaly detection
- Document the RAG-based explanation engine: anomaly context + MITRE ATT&CK knowledge base → LLM → enriched alerts with contingency plans
- Document the microservices breakdown, tech stack, auth/RBAC model, and deployment strategy
- Document open/TBD decisions (mobile framework, LogBERT training infra)

## Capabilities

### New Capabilities
- `architecture-overview`: High-level system context, deployment model (self-hosted by PyME), and service topology
- `data-pipeline`: Log ingestion pipeline — FluentBit agents → Kafka → Drain3 template extraction → LogBERT anomaly detection
- `rag-response-engine`: MITRE ATT&CK-based RAG pipeline for anomaly explanation and contingency action plans using local LLM (Ollama)
- `real-time-alerts`: SSE-based real-time dashboard updates and notification system (email/webhook)
- `auth-rbac`: JWT authentication with role-based access control (admin/analyst/viewer)

### Modified Capabilities
<!-- No existing specs to modify -->

## Impact

- **Files**: Replace contents of `architecture.md` at project root
- **Dependencies**: None — documentation only, no code changes
- **Systems**: No runtime impact — this is a docs-only change
- **Team**: All contributors gain shared understanding of architectural decisions
