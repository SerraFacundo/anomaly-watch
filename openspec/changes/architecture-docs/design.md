## Context

Anomaly Watch is a self-hosted, microservices-based log anomaly detection system for PyMEs. Currently no code exists — only project scaffolding (CONTRIBUTING.md, CI templates, OpenSpec). The team has made architectural decisions through exploration that need to be captured in a comprehensive `architecture.md` before implementation begins.

**Current state**: Generic template `architecture.md` with placeholder `[Insert X]` sections.
**Target state**: Living architecture document reflecting all decisions, serving as the primary reference for contributors and agents.

### Key architectural decisions already made:
- Self-hosted Docker Compose deployment (PyME deploys on their own infra)
- FluentBit agents on PyME machines → Kafka → central Anomaly Watch stack
- Drain3 for log template extraction → LogBERT for anomaly detection
- RAG pipeline with MITRE ATT&CK knowledge base + local LLM (Ollama) for alert explanation and contingency plans
- pgvector in Postgres (no separate vector DB) for ATT&CK embeddings
- FastAPI (Python) for REST API + SSE real-time events
- React web dashboard + mobile app (framework TBD)
- JWT auth with RBAC (admin/analyst/viewer)
- 1-year log retention with Postgres table partitioning

## Goals / Non-Goals

**Goals:**
- Document the complete system architecture as an aspirational reference
- Capture all decided and TBD elements clearly
- Enable any new contributor to understand the system in minutes
- Serve as the single source of truth for architectural decisions

**Non-Goals:**
- Implementation of any code or services
- API endpoint specifications (belongs in future specs)
- Database schema design (future change)
- CI/CD pipeline configuration (future change)
- Detailed ML model training procedures

## Decisions

### D1: Single `architecture.md` at project root vs distributed docs

**Choice**: Single `architecture.md` at project root.

**Rationale**: Project is pre-implementation. One comprehensive document is easier to maintain and discover than scattered docs. Can decompose later when services are built and warrant their own READMEs.

**Alternatives considered**:
- Per-service README.md files → no services exist yet, premature
- `docs/` folder with multiple files → adds navigation overhead for a doc-only change

### D2: Document language

**Choice**: English.

**Rationale**: Architecture docs should be accessible to all contributors and AI agents. CONTRIBUTING.md is in Spanish (team convention), but architecture is a technical reference that benefits from English.

**Alternatives considered**:
- Spanish (matching CONTRIBUTING.md) → limits AI agent comprehension, harder for international contributors

### D3: Mermaid diagrams vs ASCII art

**Choice**: Mermaid diagrams for system/flow diagrams, with ASCII fallback for simple inline diagrams.

**Rationale**: Mermaid renders natively on GitHub, is maintainable as text, and produces clear professional diagrams. ASCII art is fine for simple inline illustrations.

**Alternatives considered**:
- Pure ASCII → harder to maintain, less readable for complex diagrams
- Image files → not version-controllable, requires external tooling

### D4: Document structure

**Choice**: Custom structure tailored to Anomaly Watch, not the generic template currently in place.

**Sections planned**:
1. Project overview & deployment model
2. System architecture diagram (Mermaid C4-style)
3. Service catalog (each microservice w/ tech, responsibility, ports)
4. Data pipeline flow (FluentBit → Kafka → Drain3 → LogBERT → RAG)
5. Data stores (Postgres + pgvector)
6. Auth & security (JWT + RBAC)
7. Infrastructure & deployment (Docker Compose)
8. Development environment (Distrobox)
9. Open decisions / TBD items
10. Glossary

**Rationale**: The generic template has sections that don't apply (multiple cloud providers, multiple data stores) and misses critical sections (ML pipeline, RAG engine, MITRE ATT&CK integration). A tailored structure maps directly to Anomaly Watch's actual architecture.

## Risks / Trade-offs

- **[Aspirational doc drift]** → Architecture is aspirational (no code yet). As implementation progresses, doc may drift from reality. **Mitigation**: Mark TBD items explicitly; include "Date of Last Update" section; add architecture review to PR checklist.
- **[Over-specification]** → Documenting too many details locks in decisions prematurely. **Mitigation**: Keep TBD items as TBD; focus on high-level topology, not implementation details.
- **[Single file scalability]** → One file may become unwieldy as system grows. **Mitigation**: Start with one file; decompose into per-service docs when services are actually built.

## Open Questions

- Mobile app framework (React Native / Flutter / PWA+Capacitor) — marked TBD in doc
- LogBERT training infrastructure (local GPU / cloud / TBD) — marked TBD in doc
- Specific LLM model for RAG (Mistral 7B / Llama 3 / etc.) — marked TBD in doc
- Notification channels beyond email/webhook — marked TBD in doc
