## Why

The model layer is done and measured. `modernbert-ft` scores F1 0.930 / precision 1.000 on the fixed BGL test split, and `ml-fastpath-demo` deliberately deferred the streaming topology until that measurement existed. It exists now, and the number that matters is not the F1:

| Measurement | Value | Source |
|---|---|---|
| Throughput, CPU, one instance | **29.6 records/s** | `ml/eval/results/modernbert-ft.json` |
| p50 / p95 latency, batch of 32 | 339.6ms / 388.9ms | same |
| Model resident size | 602MB | same |

A PyME with 8 monitored hosts (nginx, sshd, systemd journal, application logs) produces roughly 50–200 lines/s at peak. **The detector is 2–7× slower than the peak it must absorb.** Every decision in this change follows from that gap.

`ARCHITECTURE.md` currently answers the transport question with Kafka in KRaft mode. That answer was written before the throughput was known and it is wrong twice over. A JVM broker costs ~1GB of RAM sitting beside a 602MB model on a single PyME box, and Kafka is engineered for throughput six orders of magnitude above 200 lines/s. It is infrastructure the deployment cannot afford, solving a problem this system does not have.

The retention policy is wrong for the same reason. §8.3 specifies one year of raw logs in Postgres, partitioned by month. At 200 lines/s that is **6.3 billion rows** on hardware chosen because it was cheap. The policy was never costed.

This change replaces both with designs sized to the measured numbers, and adds the one mechanism that actually closes the throughput gap: scoring each distinct log template once instead of scoring every line.

## What Changes

- **Drop Kafka. Drop every broker.** FluentBit's filesystem buffer becomes the queue. Agents post batches over HTTP to a new `ingest-service` and retain on disk when the central stack is unreachable. Zero broker services, zero JVM.
- **Introduce `ingest-service`** — the HTTP ingest contract, one versioned envelope shape, per-agent authentication, and the backpressure signal agents obey.
- **Introduce a template score cache.** Drain3 already assigns every line a `template_id`. Lines sharing a template score alike, so the cache scores each template once and serves repeats for free. This is the mechanism that turns 30 records/s into a usable rate — and the one that must be measured before anything is built on top of it.
- **Add an explicit drop policy.** When the detector falls behind, the system degrades in a fixed, documented order and records every discard. Silent loss is the failure mode that makes a monitoring product worthless.
- **Rewrite the retention policy.** Postgres stores flagged anomalies (raw line included in the row) and an hourly template counter table. Normal log lines are discarded after scoring. ~300k rows/year replaces ~6.3 billion.
- **Add `log-collector`** — a single-replica service holding Drain3 state, consuming from `ingest-service`, and driving the detector. Drain3 learns templates online and cannot be replicated without shared state; this change makes that a stated constraint rather than a latent bug.
- **Ship the FluentBit agent** as a templated systemd deployment with disk buffering, TLS, and a frozen record schema carrying `schema_version` from day one.

### Non-goals

Kafka is not being replaced with a different broker. Redpanda (Kafka wire protocol, no JVM) was the runner-up and is recorded in `design.md` as the documented escape hatch if fan-out or replay becomes a real requirement. Nothing in the ingest contract prevents it later.

## Capabilities

### New Capabilities
- `log-transport`: How log records travel from a monitored machine to the scoring pipeline — the agent, its buffering guarantees, the versioned wire schema, the HTTP ingest contract, authentication, backpressure signalling, and the drop policy under sustained overload.
- `score-cache`: Template-keyed score caching in front of `detector-service` — cache key composition, invalidation on model change, the bypass rules for records the cache must not answer, and the hit-rate measurement that gates the whole design.

### Modified Capabilities
- `data-pipeline`: The target topology is brokerless. Kafka, `raw-logs` and `log-templates` are removed as the specified transport. Retention changes from one year of raw logs in Postgres to anomalies plus hourly template aggregates.

## Impact

- **Files**: `ARCHITECTURE.md` (§1, §2.1, §2.2, §3.1, §3.2, §7.1, §7.2, §8.1, §8.3, §9, §10), new `services/ingest/`, new `services/collector/`, new `deploy/fluentbit/`, `docker-compose.yml`, `tools/replay.py` (retained as a load generator)
- **Dependencies**: `fastapi`, `uvicorn`, `httpx`, `psycopg` — all already present. No new runtime dependency; the change removes one (`kafka-python` was never added, and now never will be)
- **Systems**: `detector-service` is untouched — its `/score` contract already carries this design. `api`, `rag-engine`, `notifier` and the dashboard remain unimplemented and unaffected
- **Reversibility**: high for transport, low for retention. Inserting a broker later means changing one hop; discarded log lines are gone
- **Risk**: the template cache hit rate is unmeasured. If real PyME logs produce mostly unique lines, the cache wins little and the CPU problem must be attacked directly (ONNX + int8 export, already scoped as an escape hatch in `ml-fastpath-demo` task 7). Task 4 is a hard gate for exactly this reason
- **Team**: unblocks the `api` service and the dashboard, which have been waiting on a defined event path since `ml-fastpath-demo` deferred it
