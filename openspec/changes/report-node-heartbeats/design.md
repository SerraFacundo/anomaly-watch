## Context

- `ingest-service` keeps an `agents` table (`agent_id`, `host`, `last_seen`, `revoked_at`) in the same Postgres the collector writes to, and touches `last_seen` at most once a minute per agent.
- AnomalyWatch owns the contract: the collector heartbeat accepts `nodes` (up to 1000), resolves each `host` by hostname and then node `code`, and answers an outcome per node. It marks a node disconnected after 5 minutes without a newer `lastSeenAt`.

## Decisions

### 1. The collector reports, not the agents

Agents only know the ingest endpoint and their own token; they have no AnomalyWatch credential and should not need one. The collector already has a single identity in AnomalyWatch (its agent credential), and ingest already aggregates the fact. One sender, one credential.

### 2. `lastSeenAt` from `agents.last_seen`, one row per host

`MAX(last_seen)` grouped by `host`, ignoring revoked agents and agents that never reported. A host re-enrolled with a new token has two rows; the latest sighting wins.

### 3. Every minute, split at 1000

One minute keeps a live host well inside AnomalyWatch's 5-minute grace, given `last_seen` itself moves once a minute. A fleet over 1000 hosts goes out as several heartbeats in the same tick, because the backend rejects an oversized heartbeat whole. With no hosts yet, an empty `nodes` still reports the collector alive.

### 4. Opt-in and isolated

Without the three variables the reporter does not start. It runs as its own asyncio task with the blocking work in a thread, catches everything, and records the last error for `/status` without the token.

## Non-Goals

- Resource metrics (CPU, RAM, disk): agents forward logs only.
- Reporting anomalies to AnomalyWatch (`POST /api/v1/pipeline/anomalies`): separate change.
