# Anomaly Watch — Architecture

**Anomaly Watch** is a self-hosted, microservices-based log anomaly detection platform designed for PyMEs (small and medium enterprises). It collects logs from client machines, applies ML-based anomaly detection, and generates enriched alerts with MITRE ATT&CK-mapped explanations and contingency actions using a local RAG pipeline.

**Deployment model:** A single Docker Compose stack runs on the PyME's central server. FluentBit agents installed on each PyME machine forward logs to the central stack over HTTPS. There is no message broker — see §3.2.

**Date of last update:** 2026-09-07

---

## Table of Contents

- [1. System Architecture Diagram](#1-system-architecture-diagram)
- [2. Service Catalog](#2-service-catalog)
  - [2.1 Infrastructure Services](#21-infrastructure-services)
  - [2.2 Pipeline Services](#22-pipeline-services)
  - [2.3 Application Services](#23-application-services)
  - [2.4 Frontend](#24-frontend)
- [3. Data Pipeline](#3-data-pipeline)
  - [3.1 Pipeline Flow Diagram](#31-pipeline-flow-diagram)
  - [3.2 Transport](#32-transport)
  - [3.3 Drain3 Template Extraction](#33-drain3-template-extraction)
  - [3.4 Anomaly Detection Model](#34-anomaly-detection-model)
  - [3.5 Model Lifecycle](#35-model-lifecycle)
- [4. RAG Response Engine](#4-rag-response-engine)
  - [4.1 RAG Pipeline Flow](#41-rag-pipeline-flow)
  - [4.2 MITRE ATT&CK Knowledge Base Ingestion](#42-mitre-attck-knowledge-base-ingestion)
  - [4.3 Enriched Alert Structure](#43-enriched-alert-structure)
  - [4.4 LLM and Embedding Models](#44-llm-and-embedding-models)
- [5. Real-Time Alerts & Notifications](#5-real-time-alerts--notifications)
  - [5.1 SSE Real-Time Updates](#51-sse-real-time-updates)
  - [5.2 Notification Channels](#52-notification-channels)
  - [5.3 Alert Lifecycle](#53-alert-lifecycle)
- [6. Auth & Security](#6-auth--security)
  - [6.1 Authentication](#61-authentication)
  - [6.2 RBAC Roles](#62-rbac-roles)
  - [6.3 Security Considerations](#63-security-considerations)
- [7. Infrastructure & Deployment](#7-infrastructure--deployment)
  - [7.1 Docker Compose Topology](#71-docker-compose-topology)
  - [7.2 FluentBit Agent Deployment](#72-fluentbit-agent-deployment)
  - [7.3 Development Environment](#73-development-environment)
- [8. Data Stores](#8-data-stores)
  - [8.1 Postgres Schema Areas](#81-postgres-schema-areas)
  - [8.2 pgvector for ATT&CK Embeddings](#82-pgvector-for-attck-embeddings)
  - [8.3 Log Retention Policy](#83-log-retention-policy)
- [9. TBD Items](#9-tbd-items)
- [10. Glossary](#10-glossary)

---

## 1. System Architecture Diagram

C4-style system context diagram showing all major components and their interactions:

```mermaid
graph TB
    subgraph "PyME Client Machines (N servers)"
        FB1["FluentBit Agent"]
        FB2["FluentBit Agent"]
        FBN["FluentBit Agent ..."]
    end

    subgraph "Central Docker Compose Stack"
        subgraph "Infrastructure"
            PG["Postgres + pgvector"]
            NGINX["Nginx (TLS termination)"]
            OLLAMA["Ollama (LLM runtime)"]
        end

        subgraph "Pipeline Services"
            INGEST["ingest-service"]
            REDIS[("Redis queue (AOF)")]
            COLLECTOR["log-collector (Drain3 + score cache)"]
            DETECTOR["detector-service"]
        end

        subgraph "Application Services"
            API["api (FastAPI)"]
            RAG["rag-engine"]
            NOTIFIER["notifier"]
        end

        subgraph "Frontend"
            WEBAPP["web-app (React)"]
        end
    end

    subgraph "Clients"
        BROWSER["Browser"]
        MOBILE["Mobile App (TBD)"]
    end

    FB1 -- "HTTPS batches" --> NGINX
    FB2 -- "HTTPS batches" --> NGINX
    FBN -- "HTTPS batches" --> NGINX

    NGINX -- "POST /ingest" --> INGEST
    INGEST -- "RPUSH (atomic)" --> REDIS
    REDIS -- "claim_batch (1..128) + ACK" --> COLLECTOR
    COLLECTOR -- "POST /score (cache misses only)" --> DETECTOR
    DETECTOR -- "scores" --> COLLECTOR

    COLLECTOR -- "anomaly events" --> API
    COLLECTOR -- "anomalies + hourly template counts" --> PG

    API -- "REST + SSE" --> NGINX
    API -- "queries" --> PG
    API -- "RAG requests" --> RAG

    RAG -- "vector search" --> PG
    RAG -- "LLM inference" --> OLLAMA

    API -- "alert triggers" --> NOTIFIER
    NOTIFIER -- "email (SMTP)" --> EMAIL["Email Server"]
    NOTIFIER -- "webhook (HTTP)" --> WEBHOOK["Webhook Endpoint"]

    NGINX -- "HTTP/TLS" --> BROWSER
    NGINX -- "HTTP/TLS" --> MOBILE
    NGINX -- "static assets" --> WEBAPP
```

**Protocol legend:**
- **HTTPS** — FluentBit → Nginx → `ingest-service`. TLS terminated at Nginx, per-agent bearer token
- **REST** — API ↔ frontend clients, API ↔ rag-engine
- **SSE** — API → frontend clients (real-time push)
- **Internal** — rag-engine ↔ Ollama, rag-engine ↔ Postgres (pgvector)
- **SMTP/HTTP** — notifier → email servers / webhook endpoints

---

## 2. Service Catalog

### 2.1 Infrastructure Services

| Service | Technology | Responsibility | Default Port |
|---------|-----------|---------------|-------------|
| **postgres** | PostgreSQL + pgvector extension | Primary data store for templates, anomalies, alerts, users, and ATT&CK embeddings | 5432 |
| **redis** | Redis 7 (`--appendonly yes`) | Durable FIFO log queue (`pending` + `inflight` lists) decoupling `ingest-service` from the background `log-collector` worker | 6379 |
| **nginx** | Nginx | TLS termination for the agent fleet, reverse proxy, static file serving for web-app. The only host-exposed service in the stack | 80 / 443 |
| **ollama** | Ollama | Local LLM runtime for RAG inference. Runs as container, serves model via HTTP API | 11434 |

> **There is no message broker.** Kafka was specified here until the detector's
> real throughput was measured at 29.6 records/s on CPU. A JVM broker costs
> ~1GB resident beside a 602MB model on a single PyME box, and Kafka is built
> for throughput six orders of magnitude above the few hundred lines/s this
> system moves. The queue that absorbs producer/consumer mismatch is the
> FluentBit disk buffer on each monitored host (§7.2); the durability boundary
> inside the stack is a spool volume (§3.2). Conditions for reintroducing a
> broker are recorded in §9.

### 2.2 Pipeline Services

| Service | Technology | Responsibility | Default Port |
|---------|-----------|---------------|-------------|
| **ingest-service** | Python / FastAPI | HTTP entry point for agents. Authenticates the agent, validates against the wire contract, and enqueues the batch into Redis (or fallback disk spool). Acknowledges only once durably queued | 8004 (internal) |
| **log-collector** | Python / Drain3 | Background worker (`run_loop`) that claims batches (`1..128`) from Redis, extracts templates with Drain3, resolves scores through the template cache, calls `detector-service` (`1..32`) for misses, persists anomalies and hourly counters, and ACKs `inflight`. Owns the drop policy | 8005 (internal, status only) |
| **detector-service** | Python / FastAPI + transformers | Serves the anomaly model over HTTP (`POST /score`). CPU inference. The one component that loads model weights | 8003 (internal) |

> **`log-collector` runs as exactly one replica.** Drain3 learns templates
> online, so two instances build divergent template trees and the same log line
> gets a different `template_id` depending on which one saw it — breaking the
> cache key, the grouping in Postgres and the RAG context downstream. Its Drain3
> state persists to a volume; without that, a restart renumbers every template
> and the historical `anomalies` rows stop being interpretable. Scaling out
> requires shared template state, not more replicas.

> **`tools/replay.py` is not deprecated by this.** It reads a log file, parses
> it with Drain3 and calls `detector-service` directly. It remains the offline
> CI smoke test and the load generator used to size the pipeline. See §3.1.

### 2.3 Application Services

| Service | Technology | Responsibility | Default Port |
|---------|-----------|---------------|-------------|
| **api** | Python / FastAPI | REST API + SSE endpoints. Central gateway for frontend, alert management, user auth, and coordination with rag-engine | 8000 |
| **rag-engine** | Python / pgvector + Ollama | RAG pipeline: embeds anomaly context, searches pgvector for MITRE ATT&CK matches, sends prompt to Ollama LLM, returns enriched alert | 8001 |
| **notifier** | Python | Notification dispatch service. Sends alerts via email (SMTP) and webhook (HTTP POST). Additional channels TBD | 8002 |

### 2.4 Frontend

| Service | Technology | Responsibility | Default Port |
|---------|-----------|---------------|-------------|
| **web-app** | React | Real-time anomaly dashboard. Receives SSE events for live updates, displays alerts, ATT&CK mappings, and contingency plans | 3000 (dev) |
| **mobile app** | **TBD** (React Native / Flutter / PWA+Capacitor) | Mobile interface for alert monitoring and acknowledgment | TBD |

---

## 3. Data Pipeline

### 3.1 Pipeline Flow Diagram

**Agent path.** No broker anywhere in it.

```mermaid
flowchart LR
    FB["FluentBit (PyME machines)<br/>512MB disk buffer"] -->|"HTTPS batches"| NG["Nginx (TLS)"]
    NG -->|"POST /ingest"| IN["ingest-service<br/>authenticate + validate"]
    IN -->|"RPUSH, then 202"| SP[("Redis queue (AOF)")]
    SP -->|"claim_batch (1..128) + ACK"| LC["log-collector worker (Drain3)"]
    LC -->|"cache hit"| SC{{"template score cache"}}
    LC -->|"cache miss, batched"| DS["detector-service (CPU transformer)"]
    DS -->|scores| LC
    LC -->|"flagged anomalies"| PG["Postgres (anomalies)"]
    LC -->|"hourly totals"| PG2["Postgres (template_counts)"]
    LC -->|"alert trigger"| API["api service"]
    API -->|enrichment| RAG["rag-engine"]
    RAG -->|enriched alert| API
    API -->|SSE push| DASH["Dashboard"]
    API -->|notify| NOT["notifier"]
```

`api`, `rag-engine`, `notifier` and the dashboard are still unimplemented; the
path up to Postgres is built.

**Offline path.** Same parser, same scoring contract, no network. This is the CI
smoke test and the load generator used to size the pipeline — it is how the
29.6 records/s and the template-repetition numbers below were measured.

```mermaid
flowchart LR
    FILE["Log file on disk"] -->|read| RP["tools/replay.py"]
    RP -->|Drain3 parse| REC["Record: raw message + template_id + params + host + ts"]
    REC -->|"POST /score (batched)"| DS["detector-service"]
    DS -->|scores| RP
    RP -->|flagged anomalies| PG["Postgres (anomalies table)"]
```

### 3.2 Transport

Agents post batches of log records over HTTPS to `POST /ingest`. There is no
message broker; the two things a broker would have provided are provided
elsewhere:

| Concern | Mechanism |
|---|---|
| Absorbing producer/consumer rate mismatch | FluentBit's bounded filesystem buffer, on the monitored host (§7.2) |
| Surviving a restart without losing an acknowledged batch | A spool volume inside the stack: `ingest-service` writes, fsyncs and atomically renames before it answers `202`, so a reader never sees a partial batch and an agent never drops records the stack has not stored |

`log-collector` deletes a spooled batch only after it has been fully processed,
so a crash replays that batch rather than losing it. Delivery is at-least-once
and a duplicate costs one cache hit.

**Wire contract** — `services/common/wire.py`, frozen before the first agent
shipped:

| Field | Notes |
|---|---|
| `schema_version` | `major.minor`. Sent from the first record |
| `host`, `source` | Machine, and where on it (`nginx.access`, `systemd.journal`) |
| `message` | The raw log line. Capped at 16KB |
| `log_timestamp` | Kept as the string the log contained; parsing it means guessing a format and timezone per source, and a wrong guess shifts an incident by hours |
| `level` | Severity as written. Drives the drop policy (§3.6) |
| `agent_ts` | When the agent read the line. The gap from `log_timestamp` is how long a record sat in the agent's buffer |

**Version policy.** An unknown *major* is rejected with both versions named. An
unknown *field* within a known major is accepted and ignored. Agents live on
customer machines and are updated rarely, so a stack that only accepts the
newest schema is a stack that stops accepting logs from the host nobody
remembered to touch.

Batches are capped at 1,000 records: ingest is reachable by every monitored
host, so an unbounded batch is a memory-exhaustion primitive handed to whoever
holds an agent token.

### 3.3 Drain3 Template Extraction

The **log-collector** service uses the [Drain3](https://github.com/logpai/Drain3) algorithm to parse raw log messages into parameterized templates. Drain3 uses a fixed-depth tree structure for efficient online log parsing.

**Example:**

| Stage | Content |
|-------|---------|
| **Raw log** | `2026-06-07 14:23:01 INFO User admin logged in from 192.168.1.50` |
| **Extracted template** | `User <*> logged in from <*>` |
| **Parameters** | `["admin", "192.168.1.50"]` |

Template extraction gives every record a stable identity for grouping,
deduplication and RAG context.

**What gets scored is the raw message, not the template ID.** The detection model
is a pretrained language model; feeding it opaque cluster IDs would discard the
pretraining that makes it useful without training. Drain3 metadata travels
alongside the message, it does not replace it.

### 3.4 Anomaly Detection Model

The **detector-service** runs a fine-tuned open-weight transformer classifier.

> **LogBERT was dropped.** The original design called for training
> [LogBERT](https://arxiv.org/abs/2103.04475) in-house. That is a research
> project — labeled corpus, training pipeline, GPU budget — and it blocked every
> downstream component behind a model that did not exist. Fine-tuning an existing
> open-weight encoder reaches a usable signal in hours on public data. LogBERT
> remains valid prior art and could return later as one implementation behind the
> `/score` contract; it is not the mechanism.

**Model contract**

| Aspect | Value |
|---|---|
| Framing | Per-line binary classification: raw message → anomaly probability in `[0,1]` |
| Baseline (phase 0) | [`AungMoonLord/bert-log-anomaly-detection`](https://hf.co/AungMoonLord/bert-log-anomaly-detection), apache-2.0, zero training |
| Fine-tune base | [`answerdotai/ModernBERT-base`](https://hf.co/answerdotai/ModernBERT-base), apache-2.0, 8k context |
| Training corpus | LogHub 2.0 BGL, per-line alert labels ([`bolu61/loghub_2`](https://hf.co/datasets/bolu61/loghub_2)) |
| Inference | CPU only, batched. No GPU at the PyME site |
| Threshold | Configuration (`DETECTOR_THRESHOLD`), not a constant. Raw score always returned so consumers can re-threshold without re-scoring |
| Active pin | `ml/models.yaml` — repo ID **and** exact revision SHA |

**Two traps this design guards against**, both found empirically and both cheap
to fall into again:

1. **Label-index inversion.** The phase-0 baseline checkpoint ships no
   `id2label`. Assuming the usual "index 1 = positive" convention inverts every
   score it produces — verified: it scored 0.975 on a routine `systemd` line and
   0.015 on a kernel panic. The correct index is recorded per-model in
   `ml/models.yaml` and reported by `GET /model`. Our own fine-tunes write
   `id2label` into `config.json` so the ambiguity cannot recur.

2. **Label leakage from the severity field.** BGL lines carry a `Level` field
   that is very nearly the label itself (`FATAL` → ~94% anomaly, `INFO` → 100%
   normal). Training or evaluating on text containing it yields a near-perfect
   score that reflects reading one word. Only the `Content` field is scored;
   `Level` is metadata. A `level-keyword` baseline is evaluated on every run so
   the shortcut stays visible if it ever creeps back in.

**Inference flow:**
1. Receive a batch of parsed records (raw message + Drain3 metadata)
2. Tokenize the raw messages
3. Run CPU inference, take the anomaly-class probability
4. Compare against the configured threshold → flag → trigger the alert pipeline
5. Persist score, threshold, model key and model revision with each anomaly, so a
   stored anomaly stays interpretable after a model swap

### 3.5 Model Lifecycle

Models are pinned, evaluated and promoted through a fixed sequence. The point is
that swapping a model costs one config value and a benchmark run.

| Stage | Tool | Output |
|---|---|---|
| Pin & fetch | `ml/download_models.py` | Weights in a local cache; exact revision SHA written to `ml/models.yaml` |
| Dataset | `ml/data/prepare.py` | Chronologically split, labeled JSONL + a manifest hash |
| Evaluate | `ml/eval/harness.py` | Precision / recall / F1 / PR-AUC / p95 latency / model size, per candidate |
| Fine-tune | `ml/train/finetune.py` | Checkpoint + full run metadata |
| Promote | `ml/promote.py` | Gate decision; on pass, updates `default:` in `ml/models.yaml` |

**Rules that hold regardless of which model is active:**

- Revisions are commit SHAs, never branch names — a moved branch means the model
  you evaluated is not the model you deployed.
- The deployed stack runs `HF_HUB_OFFLINE=1`. No runtime downloads, so log data
  cannot egress and the served weights are exactly the evaluated weights.
- Only permissively licensed weights (apache-2.0 / MIT) — the stack is
  redistributed to self-hosting customers.
- **Promotion gate:** a candidate must beat the incumbent *and* both floor
  baselines on F1, on the same dataset manifest, without exceeding the p95
  batch-32 latency budget. Comparing numbers from different data is refused
  automatically.
- Splits are chronological. A random split lets a burst's start land in train and
  its tail in test, which inflates every metric.

---

## 4. RAG Response Engine

### 4.1 RAG Pipeline Flow

When an anomaly is detected, the RAG engine enriches the alert with explanations and contingency plans:

```mermaid
flowchart LR
    ANO["Anomaly context (templates, scores, metadata)"]
    ANO -->|embed| EMB["Embedding model"]
    EMB -->|vector query| PGV["pgvector (ATT&CK embeddings)"]
    PGV -->|top-k chunks| CTX["Retrieved ATT&CK technique chunks"]
    CTX --> PROMPT["Prompt assembly (anomaly + ATT&CK context)"]
    PROMPT -->|inference| LLM["Ollama LLM"]
    LLM -->|response| ALERT["Enriched Alert"]
```

**Flow steps:**
1. **Anomaly context** — log-collector provides the anomaly description, involved log templates, anomaly score, and source metadata
2. **Embedding** — anomaly context is embedded into a vector representation
3. **pgvector search** — similarity search against MITRE ATT&CK embeddings in Postgres, returning top-k most relevant technique/tactic chunks
4. **Prompt assembly** — anomaly context + retrieved ATT&CK chunks are combined into a structured prompt
5. **LLM inference** — Ollama generates an explanation including ATT&CK technique mapping and suggested contingency actions
6. **Enriched alert** — structured response is returned to the API for storage and delivery

### 4.2 MITRE ATT&CK Knowledge Base Ingestion

The MITRE ATT&CK framework data is ingested into pgvector for retrieval:

1. **Source** — MITRE ATT&CK Enterprise matrix (techniques, tactics, procedures)
2. **Chunking** — Each technique/sub-technique is split into chunks (description, detection guidance, mitigations)
3. **Embedding** — Chunks are embedded using a sentence transformer model
4. **Storage** — Embeddings stored in Postgres using the `pgvector` extension with IVFFlat or HNSW indexing
5. **Update cadence** — Re-ingestion on MITRE ATT&CK version updates (manual trigger)

### 4.3 Enriched Alert Structure

Each enriched alert produced by the RAG pipeline contains:

| Field | Description |
|-------|-------------|
| `anomaly_description` | Human-readable summary of what was detected (templates, scores, affected hosts) |
| `attack_techniques` | List of matched MITRE ATT&CK techniques with IDs (e.g., T1078 — Valid Accounts) |
| `attack_tactics` | Associated tactics (e.g., Initial Access, Persistence) |
| `contingency_actions` | Suggested remediation/response steps generated by the LLM |
| `confidence_score` | Anomaly detection confidence (from LogBERT) |
| `source_logs` | Reference to original log template sequence that triggered detection |

### 4.4 LLM and Embedding Models

**Embedding model (decided):** [`nomic-ai/modernbert-embed-base`](https://hf.co/nomic-ai/modernbert-embed-base)

- apache-2.0, **768 dimensions** — this fixes the `pgvector` column width (§8.2)
- Same ModernBERT family as the detector, so one architecture and one tokenizer
  story across the stack
- Pinned in `ml/models.yaml` under the `rag-embed` key, same rules as every other
  model

**Generation model:** still **TBD** — candidates include Mistral 7B and Llama 3.
Selection criteria unchanged: runs on consumer hardware, follows instructions,
produces coherent security analysis.

One candidate worth noting:
[`B4lt/gemma4-loghub-e2b-GGUF`](https://hf.co/B4lt/gemma4-loghub-e2b-GGUF) is a
Gemma-4 E2B fine-tuned on LogHub, distributed as GGUF, so it runs on the Ollama
service the stack already has. It was evaluated for *detection* and rejected —
generation is orders of magnitude slower per line than an encoder and yields no
clean probability to threshold on. For *explanation*, which is what this section
is about, those objections do not apply and it remains a live option.

---

## 5. Real-Time Alerts & Notifications

### 5.1 SSE Real-Time Updates

The **api** service exposes Server-Sent Events (SSE) endpoints for pushing real-time updates to connected clients:

- **Endpoint:** `GET /api/v1/events/stream` (authenticated, role-based)
- **Events pushed:** new anomaly alerts, alert status changes, system health updates
- **Clients:** React web dashboard and mobile app subscribe to SSE stream
- **Reconnection:** clients implement automatic reconnection with exponential backoff
- **No WebSocket needed** — SSE is unidirectional (server → client), simpler than WebSocket for this use case

### 5.2 Notification Channels

The **notifier** service dispatches alert notifications through:

| Channel | Protocol | Status | Details |
|---------|----------|--------|---------|
| **Email** | SMTP | Planned | Configurable SMTP server, alert digest with ATT&CK mapping and contingency actions |
| **Webhook** | HTTP POST | Planned | JSON payload to configurable endpoint (e.g., Slack, Teams, PagerDuty integration) |
| **Additional channels** | — | **TBD** | SMS, push notifications, or other integrations based on PyME needs |

### 5.3 Alert Lifecycle

```mermaid
stateDiagram-v2
    [*] --> Detected: log-collector flags anomaly
    Detected --> Stored: API stores in Postgres
    Stored --> Pushed: SSE push to dashboard
    Stored --> Notified: notifier sends email/webhook
    Pushed --> Viewed: user sees on dashboard
    Notified --> Viewed: user receives notification
    Viewed --> Acknowledged: user acknowledges via API
    Acknowledged --> [*]
```

1. **Detected** — log-collector flags a log sequence as anomalous
2. **Stored** — API stores the alert (with RAG enrichment) in Postgres
3. **SSE push** — API pushes event to connected dashboard clients
4. **Notification** — notifier sends email/webhook to configured recipients
5. **Acknowledged** — user acknowledges the alert through the dashboard or API

---

## 6. Auth & Security

### 6.1 Authentication

- **Mechanism:** JWT (JSON Web Tokens) for stateless authentication
- **Password storage:** bcrypt hashing (no plaintext, no reversible encryption)
- **Token flow:** login → validate credentials → issue JWT → client includes JWT in `Authorization: Bearer <token>` header
- **Token expiration:** configurable (e.g., 24h access token, 7d refresh token)
- **No external OAuth providers** — self-contained auth for self-hosted deployment simplicity

### 6.2 RBAC Roles

| Role | Dashboard | Alerts | Reports | User Management | System Config |
|------|-----------|--------|---------|----------------|---------------|
| **admin** | ✅ Full access | ✅ Manage all | ✅ Generate | ✅ Create/edit/delete users | ✅ Full configuration |
| **analyst** | ✅ Full access | ✅ Manage assigned | ✅ Generate | ❌ | ❌ |
| **viewer** | ✅ Read-only | ✅ Read-only | ❌ | ❌ | ❌ |

Roles are stored in Postgres in a `roles` / `user_roles` table structure. Role checks enforced at API middleware level.

### 6.3 Security Considerations

| Area | Approach |
|------|----------|
| **Data in transit** | TLS termination at Nginx. All external traffic encrypted via HTTPS. Internal services communicate over Docker network (not exposed externally) |
| **Data privacy** | Log data stays entirely on the PyME's own network/infrastructure. No cloud egress. Self-hosted model ensures data sovereignty |
| **API authentication** | All API endpoints require valid JWT (except login/register). Rate limiting at Nginx |
| **Secret management** | Environment variables / Docker secrets for DB credentials, JWT secret, SMTP credentials |
| **Network isolation** | Docker Compose internal network. Only Nginx port (80/443) exposed to host. All other services internal-only |

---

## 7. Infrastructure & Deployment

### 7.1 Docker Compose Topology

All services (except FluentBit agents) run in a single Docker Compose stack on the PyME's central machine:

| Service | Image | Exposed Port | Status | Notes |
|---------|-------|-------------|--------|-------|
| **nginx** | nginx:alpine | 80, 443 | built | TLS termination for agents. The only host-exposed service |
| **postgres** | pgvector/pgvector:pg16 | 5432 (internal) | built | pgvector extension pre-installed |
| **redis** | redis:7-alpine | 6379 (internal) | built | Durable log queue (`--appendonly yes`, `redis-data` volume) |
| **ingest-service** | custom (Python/FastAPI) | 8004 (internal) | built | Agent endpoint. Enqueues validated batches into Redis |
| **log-collector** | custom (Python/Drain3) | 8005 (internal, status only) | built | **Single replica** background worker. Drain3 state on a volume |
| **detector-service** | custom (Python/FastAPI) | 8003 (internal) | built | CPU inference. Model cache mounted read-only; `HF_HUB_OFFLINE=1` |
| **ollama** | ollama/ollama | 11434 (internal) | planned | LLM model runtime |
| **api** | custom (Python/FastAPI) | 8000 (internal) | planned | Behind Nginx |
| **rag-engine** | custom (Python) | 8001 (internal) | planned | Called by API |
| **notifier** | custom (Python) | 8002 (internal) | planned | Called by API |
| **web-app** | custom (React, static build) | — | planned | Served by Nginx |

All custom services share a common Docker network. Only Nginx ports are exposed
to the host. Services marked *planned* have no implementation yet and are absent
from `docker-compose.yml`, because shipping empty containers would misrepresent
what works.

Model weights are **not** baked into the detector image: they are mounted from
`./models/cache`, so the image stays small and a model swap never requires a
rebuild. Populate the cache before first run:

```bash
uv run python ml/download_models.py
```

Generate a TLS certificate for the agent endpoint (`deploy/nginx/README.md`) and
issue a token per monitored host:

```bash
uv run python tools/agent_token.py issue --host web1
```

**Healthchecks are declared in `docker-compose.yml`, not only in the Dockerfiles.**
`podman-compose` does not propagate image-level `HEALTHCHECK`, so a service
waiting on `condition: service_healthy` would block forever against a container
that never reports health. Image references are fully qualified
(`docker.io/...`) for the same reason: podman refuses to resolve short names
without a prompt.

### 7.2 FluentBit Agent Deployment

FluentBit runs **outside** the Docker Compose stack, installed directly on each
PyME client machine. It is a dumb forwarder: it reads logs, stamps the wire
envelope and posts batches. It does no parsing and holds no model, so nothing
about detection changing requires touching an agent.

- **Installation:** systemd unit (`deploy/fluentbit/anomaly-watch-agent.service`), unprivileged user, `ProtectSystem=strict`
- **Configuration:** generated per host from `deploy/fluentbit/fluent-bit.conf.template` by `tools/render_agent_config.py`, never hand-edited on the machine. Written mode 0600 — it holds a bearer token
- **Input plugins:** `systemd` (journal) and `tail` (nginx, application logs). Every input sets `storage.type filesystem`; without it that input's records live in memory and die with the process
- **Output plugin:** `out_http` to `POST /ingest` over TLS, with a per-agent bearer token
- **Buffering:** `storage.total_limit_size 512M` per host — roughly 8 hours at 200 lines/s. This buffer *is* the queue: it absorbs stack downtime and backpressure, and it stops growing rather than filling the monitored host's disk
- **Managed separately** from the central stack (different deploy lifecycle)

**Buffer sizing is an estimate.** 512MB assumes a peak of 200 lines/s that has
never been measured on a real PyME host. Correcting it is a pilot task.

**FluentBit does not read `Retry-After`.** `out_http` treats any non-2xx as a
failed flush and retries on its own backoff schedule, so the `429` is respected
in effect — the agent holds and retries — but tuning `INGEST_RETRY_AFTER_S` will
not change agent behaviour.

**systemd reports `PRIORITY` as a syslog number, not a word.** It is forwarded
as-is and mapped to a severity name in `log-collector`, so severity has one
definition in the system rather than being re-encoded in every agent config.

### 7.3 Development Environment

Local development uses **Distrobox** with an Ubuntu-based container:

- **Container:** `ubuntu-dev` (image: `quay.io/toolbx/ubuntu-toolbox:latest`)
- **Pre-installed packages:** git, vim, tmux, build-essential, curl, wget, python3, python3-pip, nodejs, npm
- **Additional tooling:** [uv](https://github.com/astral-sh/uv) (installed via init hook) for fast Python package management
- **Config:** `.distrobox/distrobox.ini` at project root
- **Usage:** `distrobox assemble create` to create the dev container, `distrobox enter ubuntu-dev` to start developing

---

## 8. Data Stores

### 8.1 Postgres Schema Areas

Postgres is the sole relational data store. Schema is organized into logical areas:

| Schema Area | Key Tables | Purpose |
|-------------|-----------|---------|
| **Templates** | `template_counts` | Per-template hourly totals per host: lines seen and lines flagged. Partitioned by month |
| **Anomalies** | `anomalies` | Detected anomalies with model scores and the full source line. Repeats of one template, on one host, in one hour collapse into a single row carrying `occurrences`, an exemplar line and the peak score. Records that bypassed the score cache are exempt and stay one row per line, because their content — the source address, the attempted username — is the finding |
| **Agents** | `agents` | Registered log agents: id, host, token hash, created, last seen, revoked |
| **Alerts** | `alerts`, `alert_enrichments` | Alert records with RAG-enriched explanations, ATT&CK mappings, contingency actions |
| **Users & Roles** | `users`, `roles`, `user_roles` | User accounts, RBAC role definitions, and role assignments |
| **Configuration** | `system_config`, `notification_config` | System-wide and per-channel notification configuration |

**The `anomalies` table records which model produced each score:**

| Column | Purpose |
|---|---|
| `message` | The raw log line that was scored |
| `template_id`, `template` | Drain3 identity, for grouping and RAG context |
| `host`, `log_timestamp` | Source machine and the timestamp as originally written |
| `score` | Raw model output in `[0,1]`, stored unthresholded |
| `threshold` | The threshold in force when the row was written |
| `model_key`, `model_revision` | Which model, at which exact revision |

Storing `model_key` / `model_revision` / `threshold` alongside the score is what
keeps historical anomalies interpretable across a model swap. A bare score from
an unknown model is not evidence of anything, and re-thresholding old data
without re-scoring it is only possible because the raw score is kept.

### 8.2 pgvector for ATT&CK Embeddings

- **Extension:** `pgvector` enabled in same Postgres instance (no separate vector DB)
- **Table:** `attack_embeddings` stores MITRE ATT&CK technique/tactic chunks as vector embeddings
- **Index:** IVFFlat or HNSW index for approximate nearest neighbor search
- **Query:** rag-engine performs `ORDER BY embedding <=> query_vector LIMIT k` for top-k retrieval
- **Dimensionality:** **768** — fixed by the choice of `nomic-ai/modernbert-embed-base` (§4.4). Column type is `vector(768)`

### 8.3 Log Retention Policy

**Raw log lines are not stored.** They are scored, counted, and discarded.

| Data | Retention | Expected volume |
|---|---|---|
| `anomalies` — flagged lines in full, raw message included | indefinite | ~300k rows/year |
| `template_counts` — hourly per-template totals | 12 months, monthly partitions dropped by `tools/retention.py` | a few million rows |
| Normal log lines | none — discarded after scoring | — |

**Why this replaced a year of raw logs in Postgres.** The previous policy
specified exactly that, partitioned by month. At 200 lines/s it works out to
**~6.3 billion rows** on hardware chosen because it was cheap; the policy had
never been costed against the volume this same document describes elsewhere.

**What is lost.** Normal-line forensics — reconstructing what a host was doing
around an incident. That is a real cost, accepted because the alternative does
not run on the target hardware. Anomaly rows carry their own full raw message,
so the lines that were flagged are never the ones missing.

**This is the one irreversible decision in the transport design.** Discarded
lines cannot be recovered later.

**Compliance fallback.** Where a customer requires raw retention, the collector
can write the raw stream to rotated gzip files on disk (`COLLECTOR_RAW_ARCHIVE_DIR`,
off by default, ~20:1 on log text). Outside Postgres, outside the dashboard,
grep-and-forensics only. It logs a warning at startup when enabled, so unbounded
retention stays a visible choice rather than a forgotten environment variable.

---

## 9. TBD Items

### Resolved

| Item | Resolution |
|------|-----------|
| **LogBERT training infrastructure** | **Closed — no longer applicable.** LogBERT was dropped (§3.4). Fine-tuning targets a single consumer GPU or a free-tier T4; inference is CPU-only, so PyMEs never need GPU hardware |
| **Embedding model for RAG** | **Closed.** `nomic-ai/modernbert-embed-base`, 768 dims, apache-2.0 (§4.4). Fixes the pgvector column at `vector(768)` |
| **Streaming topology** | **Closed — no broker.** The detector measures 29.6 records/s on CPU; a JVM broker costs ~1GB beside a 602MB model, for a load six orders of magnitude below Kafka's design point. Agent disk buffer is the queue, a spool volume is the durability boundary (§3.2). Redpanda is the recorded escape hatch if a concrete second consumer or a replay requirement ever appears |
| **Throughput at PyME volumes** | **Closed.** The template score cache collapses repeated lines into one inference per template. Measured end to end: **93.6%** hit rate on 19,839 real systemd journal lines (~460 records/s effective) and **99.83%** on 60,000 real nginx access lines — 102 inferences for 60,000 records. Against a 50–200 lines/s peak, both clear it comfortably. nginx turned out to be the *best* case for the cache, not the worst: its access-log format is rigidly uniform and Drain3 collapses the URL, giving 0.6 templates per 1,000 lines against journal's 20.3 |

### Open

| Item | Context | Impact |
|------|---------|--------|
| **Mobile app framework** | React Native, Flutter, or PWA+Capacitor | Determines mobile dev stack and code sharing with web-app |
| **LLM model for RAG generation** | Mistral 7B, Llama 3, or `B4lt/gemma4-loghub-e2b-GGUF` | Affects hardware requirements (RAM/VRAM) and response quality |
| **Additional notification channels** | SMS, push notifications, chat integrations | Extends notifier service beyond email/webhook |
| **Anomaly detection threshold** | Now a configured value (`DETECTOR_THRESHOLD`, default 0.5) rather than an unknown. What remains open is the *right* value: it has never been validated against real PyME logs, and no false-positive budget has been agreed for a customer with no dedicated SOC | Sets the false positive/negative trade-off |
| **Windows Event Log support** | **Now in scope**, not a possibility. The detector must cover nginx, sshd and systemd *plus* Windows. A Windows event is a structured XML record, not a text line, so how one becomes a single scored string is undecided — it affects both the Drain3 stage and the `message` field of the ingest contract | Widens the fine-tune corpus and adds a parser decision. Blocks nothing today, blocks the pilot on Windows hosts |
| **Client IP addresses in training data** | Public web-log corpora carry the real addresses of real people. The model has no use for them and Drain3 templatises them to `<*>` regardless | Mask before any corpus reaches a training run. Keeping them buys nothing and creates a memorisation and privacy liability |
| **Per-agent buffer size** | 512MB assumes a 200 lines/s peak that no real PyME host has produced yet | Sets how long an agent survives a stack outage before it starts refusing to buffer |
| **Alert deduplication by template** | A single mis-scored template produced 4,091 alerts in one 20k-line run. The score cache applies one judgement to every repeat, which is right for throughput and wrong for an inbox | Must land before any pilot shows alerts to a customer. Cheap, and independent of model quality |
| **Windowed detection** | Per-line today. Session/window-level is strictly better for real detection quality | ModernBERT's 8k context was chosen so this needs no base-model change |
| **Domain transfer** | **Confirmed failed, in both directions, on two independent domains.** systemd journal (19,836 lines): 21.5% flagged, 96% of those from one routine template, `Done` and `Connected to the service` scored 1.000. nginx access log (60,000 lines of real e-commerce traffic): **99.00% flagged**, the largest group being 16,917 ordinary product-page `GET`s scored 0.793. In the other direction it scores failed SSH auth, a Postgres `FATAL` and an HDFS block error at **0.0000 — normal**. F1 0.930 on BGL predicted none of this | The largest quality risk is no longer a risk, it is a measured result on two domains. A fine-tune on real logs is required, not optional. The threshold is deliberately **not** being retuned to mask it |

---

## 10. Glossary

| Term | Definition |
|------|-----------|
| **Drain3** | An online log parsing algorithm that extracts parameterized log templates from raw log messages using a fixed-depth tree. Enables structured analysis of unstructured logs |
| **LogBERT** | A BERT-based model adapted for log anomaly detection via masked-log modeling. **Prior art, not used here.** Training it in-house was the original plan and was dropped: it is a research effort that blocked every downstream component behind a model that did not exist. See §3.4 |
| **ModernBERT** | A 2024 encoder architecture (apache-2.0) with an 8k context window and faster inference than BERT at comparable size. The fine-tuning base for the detector |
| **LogHub** | A public collection of labeled system log datasets (BGL, HDFS, Thunderbird, and others) used across the log-analysis literature. Source of the training and evaluation data |
| **Promotion gate** | The rule a candidate model must clear to become the configured default: beat the incumbent and both floor baselines on F1, on the same dataset manifest, within the p95 latency budget. Enforced by `ml/promote.py` |
| **Label leakage** | When a feature in the input effectively encodes the answer — e.g. a log's severity field being nearly identical to its anomaly label. Produces impressive metrics that mean nothing. See §3.4 |
| **MITRE ATT&CK** | A globally-accessible knowledge base of adversary tactics, techniques, and procedures (TTPs) based on real-world observations. Used here to map detected anomalies to known attack patterns |
| **SSE (Server-Sent Events)** | A server push technology enabling a server to send real-time updates to a client over a single HTTP connection. Used for dashboard live updates |
| **pgvector** | A PostgreSQL extension that adds support for vector similarity search. Used to store and query MITRE ATT&CK embeddings for RAG retrieval |
| **Ingest contract** | The frozen wire envelope agents send (`services/common/wire.py`) plus the `POST /ingest` shape. Versioned from the first record because agents are updated on the customer's schedule, not ours |
| **Spool** | The directory of batch files that is the durability boundary between `ingest-service` and `log-collector`. Written with fsync and an atomic rename before a batch is acknowledged, so an agent never drops records the stack has not stored |
| **Template score cache** | Scoring each distinct Drain3 template once and serving repeats from memory. The mechanism that makes a 29.6 records/s CPU model viable at PyME log volumes |
| **Cache bypass** | Records that are always scored regardless of a cached entry — authentication outcomes, HTTP 5xx, non-zero exit codes, kernel severities. Template identity is a good default and a bad guarantee, and it is worst exactly where the system earns its keep |
| **Backpressure** | `ingest-service` answering `429` so agents retain records on their own disk. Deliberately the last resort, after shedding, because agent buffer is finite and should not be spent on records nobody would read |
| **Drop policy** | The fixed order in which load is shed under overload: sample hot never-flagged templates, then discard `INFO`/`DEBUG`, then backpressure. `ERROR`, `WARN` and bypass-matching records are never shed, and every discard is counted |
| **FluentBit** | A lightweight, high-performance log processor and forwarder. Deployed on PyME client machines to collect logs and post them to `ingest-service` over HTTPS |
| **RAG (Retrieval-Augmented Generation)** | A technique combining information retrieval (vector search) with LLM generation. Grounds LLM responses in factual, retrieved context |
| **PyME** | Pequeña y Mediana Empresa (Spanish) — Small and Medium Enterprise. The target audience for Anomaly Watch |
| **Ollama** | A local LLM runtime that makes it easy to run large language models on commodity hardware. Runs as a Docker service |
| **JWT (JSON Web Token)** | A compact, URL-safe token format used for stateless authentication. Encodes user identity and claims |
| **RBAC** | Role-Based Access Control — authorization model where permissions are assigned to roles, and users are assigned roles |
| **bcrypt** | A password hashing algorithm designed to be computationally expensive, making brute-force attacks impractical |