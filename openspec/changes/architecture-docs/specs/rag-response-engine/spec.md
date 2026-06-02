## ADDED Requirements

### Requirement: RAG architecture documentation
The architecture document SHALL describe the RAG (Retrieval-Augmented Generation) pipeline that enriches detected anomalies with explanations and contingency plans sourced from the MITRE ATT&CK knowledge base.

#### Scenario: RAG pipeline described
- **WHEN** a contributor reads the RAG engine section
- **THEN** it SHALL explain the flow: anomaly context → embedding → pgvector similarity search against MITRE ATT&CK vectors → top-k chunks + anomaly context → LLM → enriched alert with explanation and contingency actions

### Requirement: MITRE ATT&CK integration documentation
The architecture document SHALL describe how the MITRE ATT&CK knowledge base is ingested, embedded, and stored in pgvector for retrieval.

#### Scenario: Knowledge base ingestion documented
- **WHEN** a contributor reads the RAG engine section
- **THEN** it SHALL describe the process of indexing MITRE ATT&CK techniques/tactics into pgvector embeddings in Postgres

### Requirement: LLM runtime documentation
The architecture document SHALL describe the LLM runtime (Ollama) used for generating explanations and contingency plans from retrieved ATT&CK context.

#### Scenario: Ollama integration documented
- **WHEN** a contributor reads the RAG engine section
- **THEN** it SHALL describe Ollama as the local LLM runtime, note the specific model as TBD, and explain it runs as a Docker Compose service

### Requirement: Enriched alert format documentation
The architecture document SHALL describe the structure of an enriched alert produced by the RAG pipeline.

#### Scenario: Alert enrichment output described
- **WHEN** a contributor reads the RAG engine section
- **THEN** it SHALL describe that enriched alerts include: anomaly description, matched MITRE ATT&CK technique(s), and suggested contingency/remediation actions
