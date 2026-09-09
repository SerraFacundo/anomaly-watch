## Context

`ml-fastpath-demo` closed with a working model and an explicit deferral: the streaming topology would be designed once the detector's real batch size and latency were known, not before. Those measurements now exist and they invalidate the transport design in `ARCHITECTURE.md`.

**Current state**: `docker-compose.yml` runs `postgres` and `detector`. `tools/replay.py` reads a log file, parses it with Drain3 (`tools/parser.py`), batches to `POST /score`, and writes flagged rows to Postgres. No agent, no network ingest, no broker.

**Target state after this change**: FluentBit on each monitored host posts batched log records over TLS to `ingest-service`. `log-collector` parses with Drain3, consults a template score cache, calls `detector-service` for cache misses, and persists anomalies plus hourly template counters to Postgres. Everything runs in the existing Compose stack. No broker exists anywhere in the design.

### The binding constraint

One number governs the whole design:

```
modernbert-ft:  29.6 records/s,  p95 batch-32 = 388.9ms,  602MB resident
```

Peak load at a PyME is estimated at 50–200 lines/s aggregate. Every mechanism below exists to close that gap or to fail safely when it cannot be closed.

### Constraints inherited from `ARCHITECTURE.md`

- Single Docker Compose stack on one commodity machine at the customer site
- CPU-only inference; no GPU anywhere in the deployment
- Log data never leaves the PyME network
- Agents run outside the stack, on machines the operator manages separately and updates late

## Goals / Non-Goals

**Goals:**
- Log records travel from a monitored host to a stored anomaly with no message broker
- The agent never loses records when the central stack is down, and never fills the host disk
- Measure the real template repetition ratio before building anything on the assumption that it is high
- When the detector cannot keep up, the system degrades in a documented order and every dropped record is counted
- Retention that fits the hardware the product is sold to run on
- One frozen, versioned wire schema, so an agent from six months ago is still readable

**Non-Goals:**
- Replacing Kafka with a different broker (Redpanda is recorded as the escape hatch, not adopted)
- Horizontal scaling of parsing or detection — single replica is the design, not a limitation to be fixed here
- Windowed / session-level detection (still deferred; `ARCHITECTURE.md` §9)
- The `api` service, SSE push, RAG enrichment, notifier, dashboard, auth
- Agent auto-update, fleet management, or a config distribution system
- Guaranteed exactly-once delivery

## Decisions

### D1: No broker. The agent's disk buffer is the queue

**Choice**: FluentBit `out_http` with `storage.type filesystem` and a bounded per-agent disk limit, posting batched records to `POST /ingest` on a new `ingest-service`.

**Rationale**: A queue exists to absorb the difference between producer and consumer rate and to survive consumer downtime. FluentBit's filesystem buffer already does both, on the machine that produced the data, with no service to operate. Adding a central broker moves the same buffer to a place where it costs a container, memory, disk, and an operational surface — on a box that is also running a 602MB model. Kafka is designed for millions of events/s across a cluster; this system moves hundreds per second on one host. The mismatch is not marginal.

The property genuinely lost is fan-out: with a broker, a second consumer can be attached without touching the producer. There is exactly one consumer today and no concrete second one. Buying fan-out now costs real memory for a hypothetical.

**Alternatives considered**:
- *Kafka KRaft, as specified* → ~1GB JVM resident beside the model. Rejected on cost against a load six orders of magnitude below its design point.
- *Redpanda* → Kafka wire protocol in a single C++ binary, ~200MB, and FluentBit's `out_kafka` would need no change. Genuinely reasonable and the runner-up. Rejected only because it is still a broker to operate for benefits nothing currently needs. **This is the documented escape hatch**: because `ingest-service` owns a narrow contract, inserting Redpanda later changes one hop and leaves the agent config untouched.
- *NATS JetStream* → lighter still, but FluentBit has no native NATS output. A custom bridge would put our own code in the critical path, which is worse than the problem it solves.

**Reversibility**: high. `ingest-service` is one HTTP handler; a broker slots in behind it without agents noticing.

**Amendment during implementation**: the handoff from `ingest-service` to `log-collector` is a durable on-disk spool (`services/common/spool.py`), not an in-memory queue. The requirement that a batch is acknowledged only once it will survive a restart cannot be met in memory — the agent deletes its buffered copy on the 200, so a process death after an in-memory ack is silent loss with a success code attached. The spool is deliberately the smallest part of a broker that this obligation requires: durable append, ordered read, delete after processing. No replay, no consumer groups, no retention window.

### D2: Score each template once, not each line

**Choice**: `log-collector` maintains an in-memory LRU cache keyed on `(model_key, model_revision, template_id)`. A cache hit returns the stored score without calling `detector-service`. Misses are batched (32–64 records) and scored.

**Rationale**: This is the only mechanism that closes a 7× throughput gap without new hardware. Logs are overwhelmingly repetitive: `data/samples/HDFS_2k.log` collapses 2,000 lines into a few dozen Drain3 templates. If a real deployment shows 50:1 repetition, the effective rate becomes ~1,500 records/s against a raw 29.6.

The model key is part of the cache key so a model swap invalidates the cache implicitly. A cached score attributed to the wrong model would corrupt exactly the provenance that `ml-fastpath-demo` built the `anomalies` schema to preserve.

**This decision is a hypothesis, not a fact.** The repetition ratio has never been measured on anything but HDFS:

| Unique templates per 1,000 lines | Inferences needed | Verdict |
|---|---|---|
| ~40 | 40 | Cache wins 25×; design holds |
| ~300 | 300 | Cache wins 3×; marginal, needs ONNX too |
| ~900 | 900 | Cache is useless; redesign before building further |

Task 4 measures this and is a hard gate.

**Alternatives considered**:
- *Score everything, add CPU* → not available. The deployment target is one commodity box.
- *ONNX + int8 export first* → 2–4× on CPU, real but bounded, and it cannot reach 25×. Correct as the **second** lever, applied after the cache, and already scoped in `ml-fastpath-demo` task 7.
- *Use the `level-keyword` baseline as a cheap prefilter* → it flags 39.7% of records at recall 0.883 (`ml/eval/results/level-keyword.json`). Dropping the other 60% before the model sees them discards ~12% of true anomalies. Rejected as a filter; retained as a **priority signal** for the drop policy in D4, where losing INFO lines under overload is the point.

**Reversibility**: high. The cache is a lookup in front of an unchanged HTTP call; disabling it is a config flag.

### D3: The cache must not answer for every record

**Choice**: Records bypass the cache and are always scored when the template is new, when the cached entry is older than its TTL (default 1h), or when the record's extracted Drain3 parameters match a **volatility denylist** — authentication outcomes, HTTP 5xx status codes, non-zero exit codes, and kernel-level severities.

**Rationale**: The cache assumes template identity implies score identity. That assumption is exactly wrong where the system earns its keep. `Failed password for <*> from <*>` and `Accepted password for <*> from <*>` may land in the same Drain3 cluster at similarity threshold 0.4 (`tools/parser.py:51`), and the difference between them is the entire security signal. The denylist is the admission that template identity is a good default and a bad guarantee.

Bypassed records are counted separately so the bypass rate is visible; a denylist that swallows most traffic has silently deleted the cache.

**Reversibility**: high. Denylist is configuration.

### D4: Degrade in a fixed order, and count every discard

**Choice**: `log-collector` tracks its own ingest queue depth. Crossing successive thresholds triggers, in order:

1. **Shed cached-frequent templates** — sample 1 in N for templates already seen many times with a stable low score
2. **Shed low-severity records** — drop `INFO` and `DEBUG` before anything else, using the severity keywords already implemented in `ml/eval/baselines.py`
3. **Signal backpressure** — return `429` with `Retry-After` from `POST /ingest`, pushing the buffer back onto agent disk where it belongs

`ERROR`, `WARN`, and any record matching the D3 denylist are never shed. Every discard increments a counter tagged with the reason, and the counters are exposed on a status endpoint.

**Rationale**: The system will be overloaded; that is arithmetic, not speculation. The choice is between a designed degradation and an emergent one. Backpressure is deliberately the last step rather than the first: agent disk is the right place for a backlog, but reaching for it before shedding INFO noise wastes the buffer on records nobody would have read.

Uncounted drops are the specific failure that makes a monitoring product actively harmful — an operator reading a quiet dashboard cannot distinguish "nothing happened" from "we threw it away".

**Reversibility**: high, all thresholds are configuration.

### D5: `log-collector` is single-replica by design

**Choice**: Exactly one `log-collector` instance. Drain3 state persists to a volume via `FilePersistence`, already supported at `tools/parser.py:58` and currently unused.

**Rationale**: `LogParser` wraps Drain3's `TemplateMiner`, which learns templates online — the docstring at `tools/parser.py:46` already says the instance must see the whole stream. Two replicas build two divergent template trees, so the same log line gets different `template_id`s depending on which replica saw it. That breaks the cache key, breaks grouping in Postgres, and breaks RAG context downstream. Without persistence, a restart renumbers every template and the historical `anomalies` rows stop meaning anything.

One replica is sufficient: it is bounded by the detector at 29.6 records/s, and Drain3 parses orders of magnitude faster than that. Scaling out later requires shared template state (Redis persistence plus a lock), not more replicas.

**Reversibility**: medium. Moving to shared state is a real change, but the `template_id` contract stays put.

### D6: Freeze the wire schema now, version it from the first record

**Choice**: One envelope, frozen before any agent ships:

```
{schema_version, host, source, log_timestamp, level, message, agent_ts}
```

`ingest-service` rejects unknown major versions with `400` and accepts unknown *fields* silently within a known major version.

**Rationale**: Agents live on the customer's machines and are updated on the customer's schedule, which is to say rarely. A stack that only accepts the newest schema is a stack that stops accepting logs from the host nobody remembered to touch. Versioning is nearly free before the first deployment and expensive to retrofit across a fleet.

### D7: Postgres stores anomalies and aggregates, never raw normal lines

**Choice**: Replace `ARCHITECTURE.md` §8.3. Persist:
- `anomalies` — unchanged from `ml-fastpath-demo`, raw message included in the row, retained indefinitely
- `template_counts` — `(template_id, hour, host, count, anomaly_count)`, retained one year

Normal lines are discarded after scoring.

**Rationale**: §8.3 specifies a year of raw logs partitioned by month. At 200 lines/s that is ~6.3 billion rows on hardware chosen for being cheap. It was never costed against the volume the same document describes. The realistic figures: anomalies at the observed 5.8% flag rate on a shed-and-cached stream produce roughly 300k rows/year; hourly template counters produce a few million at most.

The information actually lost is normal-line forensics — reconstructing what a host was doing around an incident. That is a genuine cost, accepted because the alternative does not run. Where a customer requires it for compliance, the fallback is compressed rotated files on disk (~20:1 on log text), outside Postgres and outside the dashboard.

**Reversibility**: **low.** This is the one irreversible decision in the change. Discarded lines cannot be recovered. The rotated-file fallback exists precisely because the default cannot be undone after the fact.

### D8: `tools/replay.py` stays, as a load generator

**Choice**: Keep the replay harness. It remains the CI smoke test (`--assert-anomalies`) and becomes the load generator that drives the task 4 measurement and the task 8 backpressure test.

**Rationale**: It already speaks the exact contract under test and runs offline. Deleting a working, wired-up test to celebrate the arrival of the real transport would be a self-inflicted regression.

## Risks / Trade-offs

| Risk | Mitigation |
|---|---|
| **Template repetition ratio is low on real PyME logs**, so the cache does not close the throughput gap | Task 4 is a hard gate before any dependent work. If the hit rate misses 90%, stop and apply ONNX + int8 before continuing. Measured, not assumed |
| Cache returns a stale score for a template whose meaning changed (auth success vs failure) | D3 denylist plus TTL. Bypass rate is counted so an over-broad denylist is visible rather than silent |
| No broker means no replay: a `log-collector` crash mid-batch loses in-flight records | Accepted. Agent disk buffer covers stack downtime; only the in-flight batch is at risk. `ingest-service` acknowledges only after the batch is durably handed off |
| Discarding normal lines is irreversible (D7) | Compressed on-disk archive as an opt-in fallback for compliance customers. Decision is documented in `ARCHITECTURE.md`, not buried in code |
| Drain3 single-replica becomes the bottleneck as customers grow | It is bounded by the detector, which is 100× slower. Revisit only when the detector stops being the constraint |
| Model trained on BGL (HPC logs) meets nginx / sshd / systemd for the first time | Unchanged and still the largest quality risk in the project (`ARCHITECTURE.md` §9, domain transfer). Task 10 pilot is the first real measurement; expect worse F1 and budget a domain fine-tune |
| Agent ships logs unencrypted over the customer LAN | TLS plus a per-agent bearer token is in scope here (task 2). The current design has no auth at all, which would not survive an audit |

## Migration Plan

Nothing is deployed; there is no data to migrate. Rollout is sequenced so the gate comes before the build:

1. **Freeze the contract.** Wire schema and `/ingest` shape as shared Pydantic models. Everything downstream depends on it.
2. **Measure the cache hypothesis.** Drive `tools/replay.py` at realistic rates over the most varied log sample available, record templates-per-1000-lines and the projected hit rate. **Gate: below 90%, stop and re-plan.**
3. **Build the path.** `ingest-service`, then `log-collector` reusing `tools/parser.py` and the existing `post_batch` / `store_postgres` logic from `tools/replay.py`.
4. **Add the safety mechanisms.** Cache bypass rules, drop policy, counters, `429` backpressure. These need real lag numbers to tune, so they come after step 3.
5. **Ship one agent.** FluentBit on a single pilot host. First real-world log line, first domain-transfer evidence.
6. **Update `ARCHITECTURE.md`** in the same change, not afterwards.

**Rollback**: at any point before step 5, `tools/replay.py` still runs the full demo path from a file. After step 5, stopping the agent returns the system to that state.

## Open Questions

- What is the actual template repetition ratio on nginx / sshd / systemd logs? Task 4 answers it, and the answer can invalidate D2.
- What per-agent disk buffer size is right? 512MB is a starting guess covering ~8 hours at 200 lines/s uncompressed; the pilot should confirm it.
- Should `template_counts` aggregate hourly or per-minute? Hourly is cheaper; per-minute is what a burst-detection feature would need later.
- Does the RAG engine need normal-line context around an anomaly? If yes, D7 needs a bounded ring buffer of recent lines per host, and that should be decided before agents ship rather than retrofitted.
- Who rotates the per-agent bearer tokens, and how, on machines the operator visits rarely?
