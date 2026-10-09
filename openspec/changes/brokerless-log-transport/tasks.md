## 1. Freeze the contract

- [x] 1.1 Define the wire record envelope as shared Pydantic models: `schema_version`, `host`, `source`, `log_timestamp`, `level`, `message`, `agent_ts` — `services/common/wire.py`, `WireRecord`. Deliberately not the detector's `LogRecord`: the agent carries no template and no cluster id, so Drain3 can change without touching a deployed agent
- [x] 1.2 Define the `POST /ingest` request/response shape: batch of records in, per-record accept/reject out, offending record positions named on validation failure — `IngestRequest` / `IngestResponse` / `RecordProblem`, plus `BackpressureResponse` for the 429 body. Batch capped at 1000 records and messages at 16KB: ingest is reachable by every monitored host, so an unbounded batch is a memory-exhaustion primitive
- [x] 1.3 Place the models where both `services/ingest/` and `services/collector/` import them, so the contract has exactly one definition — `services/common/`, imported via the same `sys.path` idiom `services/detector/app.py` already uses
- [x] 1.4 Decide and document the `schema_version` policy: unknown major rejected with both versions named, unknown fields within a known major ignored — `check_major()` + `SchemaVersionError` carrying received and supported; `extra="ignore"` implements the minor policy. Verified both ways: a record with an unknown field is accepted, `schema_version="2.0"` is rejected
- [x] 1.5 Write the contract down in the change before writing any service, so tasks 3 and 5 build against a fixed target — `design.md` D6 and `specs/log-transport/spec.md` ("Versioned wire schema"); `wire.py` was written before any service and no service existed when tasks 1.x closed

## 2. Ingest authentication and transport security

- [x] 2.1 Add per-agent bearer token authentication to `POST /ingest`; a batch with an invalid credential is rejected whole, no partial processing — `services/ingest/auth.py` + `require_agent` dependency. The store **fails closed**: if Postgres is unreachable the token does not resolve and the request is denied, because an ingest endpoint that accepts everything while its auth store is down is an open relay
- [x] 2.2 Store agent credentials in Postgres (`agents` table: agent id, host, token hash, created, last seen, revoked) — tokens stored as SHA-256, never plaintext. Plain SHA-256 rather than a password KDF is deliberate: the token is 32 random bytes, so there is no low-entropy secret for a KDF to protect. Rejections are cached alongside successes so a host stuck retrying a revoked token cannot turn into a load generator against the auth store, and `last_seen` writes are throttled to once a minute per agent
- [x] 2.3 Terminate TLS at Nginx in front of `ingest-service`; `ingest-service` itself stays internal-only on the Compose network — `deploy/nginx/nginx.conf`, TLS 1.2/1.3, port 80 redirects and never proxies. `/status` is deliberately **not** exposed: backlog depth tells an outsider exactly when the pipeline is blind. Certs are gitignored with generation and rotation documented in `deploy/nginx/README.md`
- [x] 2.4 Write the token issuance and revocation procedure — `tools/agent_token.py issue|list|revoke`. **Rotation answered**: `issue` mints a second credential for a host without revoking the first, so an operator installs the new token, confirms `last_seen` is moving in `list`, and only then revokes the old one. Overlap rather than cutover, because a cutover on a host nobody can reach is a host that silently stops shipping. The token is printed once and only its hash is kept

## 3. `ingest-service`

- [x] 3.1 Create `services/ingest/` following the layout and env-var config convention of `services/detector/` — `config.py`, `auth.py`, `app.py`, `Dockerfile`. No torch in the image: this is the first thing an agent talks to and the last thing that should be slow to restart during an incident
- [x] 3.2 Implement `POST /ingest` — authenticate, validate, hand the batch to the collector, acknowledge only after durable handoff.

  **Design correction.** This task and 5.4 originally said the handoff was a *bounded in-process queue*. That contradicts the spec scenario "an acknowledged batch is never silently lost": an in-memory queue dies with the process, and the agent has already dropped its copy on the 200. Implemented as a durable on-disk spool instead (`services/common/spool.py`) — write to `.tmp`, `fsync`, atomic `rename`, then acknowledge. A reader therefore only ever sees complete batches, and a crash leaves a sweepable `.tmp` rather than a half-batch that parses as a short one. Batches are deleted only after processing, so a crash replays rather than loses; the duplicate costs one cache hit. Task 5.4 is reworded to match

- [x] 3.3 Implement `GET /healthz` and `GET /readyz` matching the semantics already established in `services/detector/app.py`, plus `GET /status` for backlog depth
- [x] 3.4 Implement the `429` + `Retry-After` backpressure response, driven by spool depth. Depth is read from the filenames (record count is encoded in the stem) rather than by opening every pending file — depth is checked on every request, and reading the backlog to measure the backlog is how an overloaded system gets more overloaded
- [x] 3.5 Add `ingest-service` to `docker-compose.yml`, internal-only, behind Nginx; spool on a named volume; `nginx` is now the only host-exposed service in the stack
- [x] 3.6 Verify: `tools/smoke_ingest.py`, 17 checks, all passing — no token and bad token both rejected with nothing spooled, unknown major rejected naming both versions, unknown field accepted, batch on disk at ack time with no `.tmp` left behind, offending record index named, saturated spool returning 429 with `Retry-After`.

  **Also verified inside Compose against real Postgres.** The earlier note here claimed no container runtime existed on this machine — that was wrong. `which podman` fails *inside the distrobox*, but the host runs podman 5.8.4, reachable with `distrobox-host-exec`. Over the container network with a token issued by the real CLI: no token `401`, bad token `401`, valid batch `202 {"accepted":2}`, `schema_version 2.0` rejected `400` naming both versions, unknown field accepted `202`. Spool files landed on the volume with `agent_id` stamped, and `agent_token.py list` showed `last_seen` moving.

  Two deployment fixes this surfaced: image short-names had to be fully qualified (`docker.io/...`) for podman, and **`podman-compose` does not propagate the Dockerfile `HEALTHCHECK`**, so `condition: service_healthy` waited forever on a container that never reported health. Healthchecks are now declared in `docker-compose.yml` itself

## 4. GATE — measure the template repetition ratio

- [x] 4.1 Extend `tools/replay.py` to report unique Drain3 templates per 1,000 lines and the projected cache hit rate — `--template-stats` parses only, no scoring, so the measurement that gates the cache does not depend on the service whose load it is measuring. The same report also prints at the end of a normal scoring run
- [x] 4.2 Collect the most varied real log sample available — **118,817 lines of real systemd journal** from the host (`journalctl -D /run/host/var/log/journal`), covering 40+ units. **nginx and sshd were not available on this machine**, which is a real gap in the sample and is carried forward to task 10.2
- [x] 4.3 Measure templates-per-1000 and projected hit rate per source:

  | Source | Records | Unique templates | Per 1,000 | Hit ceiling |
  |---|---|---|---|---|
  | Full host journal | 118,817 | 2,409 | 20.3 | **97.97%** |
  | `systemd` only | 17,303 | 609 | 35.2 | **96.48%** ← worst source |
  | `ubuntu-box` only | 39,217 | 662 | 16.9 | 98.31% |
  | `gnome-shell` only | 36,501 | 123 | 3.4 | 99.66% |
  | `pipewire` only | 1,261 | 3 | 2.4 | 99.76% |
  | HDFS sample (contrast) | 2,000 | 17 | 8.5 | 99.15% |

  Steady state matters more than the aggregate, because the aggregate includes a cold cache. New templates per 20k-line window: 642 → 286 → 343 → 387 → 358, i.e. a **flat ~98.2% steady-state hit rate that does not decay** across 100k lines. Discovery does not converge to zero — a real deployment keeps meeting new templates forever — but it stays far below the miss rate that would matter.

- [x] 4.4 Effective throughput: at the worst measured source (96.48%, `systemd`) the miss rate is 3.52%, so 29.6 records/s raw becomes **~840 records/s effective**. At the full-journal 97.97% it is **~1,460 records/s**. Against the 50–200 lines/s peak estimate that is 4–7× headroom instead of a 2–7× deficit
- [x] 4.5 **Decision: PROCEED.** Every measured source clears the 90% gate, the worst by 6.5 points, and the steady-state rate is stable rather than degrading. ONNX + int8 stays unbuilt and stays available (task 12.1) — the cache alone closes the gap, so spending accuracy on quantization now would buy headroom nothing needs.

  Three caveats recorded rather than glossed:
  1. **These are ceilings.** No TTL expiry, no denylist bypass, unbounded cache. The delivered rate is lower and only task 10.2 measures it.
  2. **No nginx, no sshd.** The sample is systemd-heavy from a desktop, not a PyME server. HTTP access logs carry high-cardinality URLs and could parse far worse. This is the single biggest reason the gate is provisional.
  3. **Cache sizing falls out of the measurement**: 2,409 templates over 119k lines means a 10,000-entry LRU covers this workload with room to spare. That is the default for task 6.1, and it is a measurement rather than a guess

## 5. `log-collector`

- [x] 5.1 Create `services/collector/`, single replica, no HTTP port beyond a status endpoint — `config.py`, `cache.py`, `pipeline.py`, `store.py`, `archive.py`, `app.py`. Processing lives in `pipeline.py`, separate from the service shell, so the cache and the drop policy can be tested without a database, a model or a network — they are the two behaviours that only misbehave under load
- [x] 5.2 Wire `tools/parser.py` `LogParser` in with `FilePersistence` enabled against a Compose volume — the `persistence_path` parameter existed and was unused; now driven by `COLLECTOR_DRAIN_STATE` onto the `drain-state` volume
- [x] 5.3 Verify template identity survives a restart — and verify it against the actual failure mode, not a trivially-passing case. A parser restarted on the *same* line sequence reassigns the same ids with or without persistence, so that proves nothing. Restarting into traffic that did not exist before the crash:

  ```
  with persistence   : template_id 1 -> 1
  without persistence: template_id 1 -> 2
  ```

  Without the state file the same log line silently changes identity after a restart, which breaks the cache key and orphans every historical `anomalies` row that referenced the old id
- [x] 5.4 Consume the durable spool (`services/common/spool.py`) written by `ingest-service`, deleting each batch only after it is fully processed, and expose depth (task 3.4 and task 8 both read it). **Reworded from "bounded in-process queue" — see task 3.2 for why an in-memory handoff cannot satisfy the durable-ack requirement**
- [x] 5.5 Reuse the batching and `POST /score` call from `tools/replay.py:40` rather than writing a second client for the same contract — same shape, same batching, `detector-service` untouched
- [x] 5.6 Reuse the anomaly persistence logic from `tools/replay.py:51`, including model key, revision and threshold on every row — `store.py`, plus `agent_id`, `source` and `from_cache`. `from_cache` is new and earns its place: it marks rows whose score came from a template match rather than a fresh inference, which is the first thing to check if a false positive needs explaining
- [x] 5.7 Add `log-collector` to `docker-compose.yml` with the single-replica constraint and its reason written in a comment, matching how the existing file documents its own choices

## 6. Score cache

- [x] 6.1 Implement the LRU cache keyed on `(model_key, model_revision, template_id)`, with a configurable size and TTL (default 1h). Default capacity 10,000 from the task 4.3 measurement, not from a round number

  **Bug found and fixed while testing.** The first implementation looked up every record in a batch *before* scoring any of them, so 200 identical lines arriving together all missed and all went to the detector — the cache only worked between batches, and intra-batch repetition is the common case, not the corner case. Now one representative per template is scored and the result fans out to the group. Measured on the smoke test: 200 records, **1 inference**. Grouping is disabled along with the cache (6.6), so "cache off" still means every record reaches the model

- [x] 6.2 Verify model-swap invalidation — model key and revision are part of the cache key, so a swap invalidates implicitly rather than depending on anyone remembering to flush. Verified: changing the revision re-scores instead of serving the previous model's score
- [x] 6.3 Implement the volatility denylist bypass — `needs_bypass()` in `cache.py`, matched against message and extracted Drain3 params: authentication outcomes, HTTP 5xx, non-zero exit codes, kernel severities.

  **Bug found by real nginx traffic, fixed.** The HTTP 5xx pattern matched any bare 500–599 number, so a *response size* of 567 bytes read as a 5xx status. On 30k lines of production e-commerce traffic that put the bypass rate at **12.65%** instead of ~0, which quietly ate the cache: bypassed records skip both the cache and the grouping, so they each cost an inference and each write their own row. Anchored to where a status code actually appears (`" 500 `, `status=502`, `500 Internal Server Error`). Re-measured on 60,000 lines: bypass **0.02%**, hit rate **99.83%**, **102 inferences for 60,000 records** — down from 3,462 for 30,000. Regression tests added for the exact false match
- [x] 6.4 Always score first-seen templates and expired entries; records Drain3 could not cluster are misses by construction
- [x] 6.5 Expose hit rate, bypass rate, entry count and effective records/s on `GET /status`.

  Added a `coalesced` counter after the 6.1 fix exposed a second problem: the hit rate read **0%** while the pipeline was doing 1 inference for 200 records, because intra-batch duplicates were misses at lookup time. A hit rate that says 0% when 99.5% of records cost nothing is worse than no metric — it would send an operator hunting for a broken cache that is working. Hit rate now measures inference avoided, which is the question it exists to answer

- [x] 6.6 Add the config flag that disables the cache entirely, and confirm stored anomalies are identical in shape with it on and off — verified: same anomalies, same row shape, 33 inferences with the cache off versus 2 with it on
- [x] 6.7 Test the case the cache exists to get wrong — 40 `Failed password` / `Accepted password` lines: **all 40 scored, none served from cache**, all 40 counted as bypasses. Plus HTTP 500, non-zero exit codes and kernel panic bypassing, and a routine line correctly *not* bypassing (a denylist that matches everything is a deleted cache)

## 7. Retention and aggregates

- [x] 7.1 Add the `template_counts` table: `(template_id, hour, host, count, anomaly_count)`, partitioned by month so retention is a `DROP` rather than a `DELETE` across a table the database is also serving reads from
- [x] 7.2 Have `log-collector` increment counters for every record, including the ones it discards — counting happens **before** the shed check, deliberately. A dropped record is still traffic, and without that ordering an operator cannot tell "this hour was quiet" from "this hour was thrown away"
- [x] 7.3 Confirm no normal log line reaches Postgres outside an aggregate — `store.write` takes anomalies and counters only; there is no code path that inserts a below-threshold line. Anomalies and counters go in one transaction, so the dashboard can never show anomalies against an hour that claims no traffic. Verified against the live database after the load test: `anomalies` 17,489 rows, `template_counts` 642 rows covering 22,839 counted lines — the counter total exceeds the anomaly total by exactly the shed and below-threshold records, which is the "a dropped record is still traffic" guarantee holding outside a unit test
- [x] 7.4 Add the monthly partition-drop job for `template_counts` — `tools/retention.py`, idempotent, with `--dry-run`. Creates partitions two months ahead as well as dropping old ones: a missing partition is not a slow insert, it is an insert that fails outright at the month rollover.

  **Bug found only against a real database**: `CREATE TABLE ... PARTITION OF ... FOR VALUES FROM (%s)` fails with `UndefinedParameter` — Postgres does not accept bind parameters in DDL. The collector crashed at startup on it. Rebuilt with `psycopg.sql.Literal` composition rather than an f-string, even though those values come straight back from the database, because a DDL path that concatenates strings is one refactor away from taking them from somewhere else. Verified live: three monthly partitions present, retention job runs clean
- [x] 7.5 Implement the compliance fallback as opt-in: compressed rotated JSONL on disk, outside Postgres, off by default — `archive.py`, one gzip file per UTC day, enabled only by setting `COLLECTOR_RAW_ARCHIVE_DIR`. Logs a warning at startup when on, because unbounded retention should be a visible choice rather than a forgotten environment variable
- [ ] 7.6 Record the measured row-growth rate per day so the one-year projection in `design.md` can be checked against reality rather than arithmetic — **blocked on task 10**, needs real traffic over real time

## 8. Backpressure and drop policy

- [x] 8.1 Implement the three shedding stages in order — `ShedPolicy` in `pipeline.py`: sample hot never-flagged templates, then drop `INFO`/`DEBUG`, then `429` from `ingest-service`. Backpressure is last on purpose: reaching for the agent's disk buffer before shedding INFO noise spends a finite resource on records nobody would read
- [x] 8.2 Reuse the severity keywords already implemented in `ml/eval/baselines.py` — `classify_level()` imports `_SEVERITY_PATTERN` rather than defining a second notion of "serious" that drifts from the one model evaluation is measured against. A record's declared `level` wins; the regex is the fallback when the source does not set one
- [x] 8.3 Guarantee `ERROR`, `WARN` and denylist-matching records are never shed — two independent protections, and the second matters: an **INFO-level** `Failed password` is exactly the line worth keeping, and severity alone would have dropped it. Verified under stage-2 shedding with a mixed batch
- [x] 8.4 Add per-reason discard counters and expose them on the status endpoint
- [x] 8.5 Load-test through the full agent path. Added `tools/replay.py --ingest --token --rate`, which posts raw lines to `ingest-service` instead of scoring directly — the point is to behave like an agent so parsing, caching, shedding and backpressure all happen where they will in production rather than being short-circuited.

  **Sustained run**, 19,836 real journal lines at 102 lines/s (roughly 3.4× the detector's 29.6 records/s raw rate): all accepted, **zero throttling**, spool drained to 0. The cache absorbed the difference exactly as designed.

  **Overload run**, same file offered at 66,665 lines/s with thresholds lowered to make the backlog reachable: 3,000 accepted — the configured backpressure threshold, to the record — and **16,836 throttled with 429**. A real agent would be holding those on its own disk. Shedding fired (`Shed stage 0 -> 2 at spool depth 3000`) and 2,677 records were discarded by level, every one counted

- [x] 8.6 Confirm an operator can tell "no anomalies" apart from "records discarded" using only the status endpoint — and it did **not** read correctly at first.

  With the backlog drained to 0, `/status` kept reporting `shed_stage: 2`. `update_depth` only ran when there was work to do, so an idle pipeline reported itself as shedding — the exact confusion this task exists to prevent, and the same class of fault as the hit-rate-0% bug in 6.5. Fixed by recomputing the stage even when the spool is empty. Verified after the fix: `backlog 0 -> stage 0`

## 9. FluentBit agent

- [x] 9.1 Write the agent config template — `deploy/fluentbit/fluent-bit.conf.template`: `systemd` journal input plus `tail` for nginx access and error
- [x] 9.2 Configure `storage.type filesystem` with a 512MB per-agent limit and the record envelope from task 1.1.

  **Correction on the systemd input**: it sets `PRIORITY` as a syslog *number* (0–7), not a word. Forwarded as-is, that would have landed in neither the protected nor the sheddable severity set — so journal records would have been silently un-sheddable, disabling the drop policy on the source producing most of the traffic. Mapped in `classify_level()` instead of in each agent config, so severity has one definition in the system. Verified: `3 → ERROR`, `6 → INFO`, `7 → DEBUG`

- [x] 9.3 Configure `out_http` with TLS, bearer token, batching and retry-with-backoff — `Retry_Limit False` so records are never dropped by giving up, `tls.verify On` with the CA path.

  **`Retry-After` is not honoured, and the config says so.** FluentBit's `out_http` retries any non-2xx on its own backoff schedule and does not read the header. The 429 is still respected in effect — the agent holds and retries — but tuning `INGEST_RETRY_AFTER_S` will not change agent behaviour, and someone would otherwise have spent an afternoon discovering that. Recorded in the config comment rather than in a commit message nobody reads

- [x] 9.4 Package as a systemd unit with a documented install procedure — `anomaly-watch-agent.service` (unprivileged user, `StateDirectory`, `ProtectSystem=strict`), `tools/render_agent_config.py`, and `deploy/fluentbit/README.md`. The renderer refuses to write a config with an unsubstituted placeholder and chmods to 0600, because a config holding a bearer token that authenticates as the literal string `@@AGENT_TOKEN@@` fails in a way nobody notices until the host has been silent for a week. Verified end to end: rendered config has zero placeholders left and mode 600
- [ ] 9.5 Test the buffering guarantee end to end — **still open, but not for the reason first recorded.** The stack does run (see 3.6); what has not been exercised is a real FluentBit process with a real filesystem buffer, because no agent has been installed on a host yet. The *stack* side of the guarantee is verified: `ingest-service` acknowledges only after fsync+rename, and the collector replays an unprocessed batch rather than losing it. What is untested is FluentBit's own retention across a stack outage
- [ ] 9.6 Test the buffer limit past 512MB — open with 9.5, needs a real agent. Note for whoever runs it: `storage.total_limit_size` was under `[SERVICE]` in the template, where FluentBit ignores it, so the buffer was in fact unbounded. Moved to `[OUTPUT]`, where it belongs
- [ ] 9.7 Local multi-node demo — `deploy/demo/` (overlay `compose.demo.yml`, `demo.sh`, `loggen.py`, guion in `README.md`): three real FluentBit agents (`web1`, `web2`, `auth1`) tailing generated nginx/sshd logs, TLS against the self-signed cert via `tls.ca_file`, one token each from `tools/agent_token.py`. Written and statically checked (`compose config`, lint, generator output); **not yet run** — the cloud session cannot pull images. Running it closes most of 9.5 and 10.1 on one machine

## 10. Pilot on one host

- [ ] 10.1 Deploy the agent to a single real machine and watch the full path: agent → ingest → collector → detector → Postgres. **Everything after the agent is verified end to end** (3.6, 7.3, 8.5, 10.2); what remains is FluentBit itself on a host that is not this one
- [x] 10.2 Record the real template repetition ratio and cache hit rate on real traffic, and compare against the task 4 projection — done on this development host rather than a pilot machine, but through the **real pipeline**, not a projection:

  | | Task 4 projection | Delivered through the pipeline |
  |---|---|---|
  | Records | 20,000 journal lines | 19,839 parsed |
  | Templates | 642 | 642 |
  | Hit rate | 97.97% ceiling | **93.56%** |
  | Inferences | — | 1,277 of 19,839 |
  | Effective rate | 1,460 rec/s | **459.9 rec/s** |

  The 4.4-point shortfall against the ceiling is the bypass rate (3.34%) plus TTL expiry — exactly the two effects task 4.5 named when it recorded the projection as a ceiling rather than a promise. Still comfortably over the 90% gate, and 459.9 rec/s against a 50–200 lines/s peak is real headroom.

  **Still owed**: nginx and sshd traffic. This is systemd journal on a desktop. The caveat from 4.5 stands untouched
- [x] 10.3 Record the real anomaly flag rate — rerun on `modernbert-ft` after 10.3b, 19,836 real journal lines at 153 lines/s:

  | | `baseline-bert` | `modernbert-ft` |
  |---|---|---|
  | Records parsed | 19,839 | 19,836 |
  | Inferences | 1,277 | 1,275 |
  | Hit rate | 93.56% | **93.57%** |
  | Bypass rate | 3.34% | 3.34% |
  | Effective rate | 459.9 rec/s | **460.5 rec/s** |
  | **Flag rate** | 88% | **21.47%** |

  The transport numbers are identical to three decimal places across a complete model swap, which is the point of the cache being keyed on template rather than on content: it is model-independent by construction, and the swap proved it rather than the design claiming it.

  **21.47% is still a failure.** One in five lines of a routine desktop journal raised as an anomaly is unusable for a customer with no SOC. See 10.4

- [x] 10.3b Restore `modernbert-ft` into `models/cache/` so the registry default resolves inside the container — checkpoint recovered from an archive. Only the final checkpoint was extracted (~575MB): the two `checkpoints/checkpoint-*` directories in the archive carry `optimizer.pt` and `rng_state`, which exist to *resume training* and are never touched at inference. `config.json` carries `id2label {0: Normal, 1: Anomaly}`, so the anomaly index resolves automatically as the registry claimed.

  **Path resolution fixed properly rather than papered over.** `repo_id` named a repo-relative training-output path, which resolved from `uv run` at the repo root and nowhere else — the model cache is mounted at a different path inside the container, so no single literal path could work in both. `ml/scorer.py` now resolves `repo_id` against `cache_dir` when it names a directory there, and the registry entry is just `modernbert-ft`. Verified in the container: `GET /model` returns `key modernbert-ft`, `anomaly ix 1 (matched label 'Anomaly')`, `eval f1 0.9304`
- [x] 10.4 Hand-review the top flagged anomalies. **Precision has collapsed. That is the finding, and the threshold is not being touched to hide it.**

  Every one of the 12 highest-scoring anomalies scored exactly 1.000, and every one is routine desktop noise:

  ```
  1.000  gnome-session-service: Could not create transient scope for PID 4217
  1.000  input-remapper-autoload.desktop: Autoloading all
  1.000  gnome-shell: libinput error: ELAN0712 Touchpad: kernel bug
  ```

  Grouped by template, the shape of the failure is stark — **4,091 of 4,259 anomalies (96%) are a single template**: `GPU process launch failed` from a desktop app. The rest are touchpad errors, `Unmounting ...`, `Connected to the service`, `Done`, and `distrobox up`. `Done` is being raised as a security anomaly at score 1.000.

  And it fails in the other direction at the same time. `ml/smoke_score.py` on the restored checkpoint scores **4/8 directional agreement**: `Failed password for invalid user root`, `postgres FATAL: could not write to file`, and an HDFS `DataNode writeBlock` error all score **0.0000 — normal**. Only the kernel panic is caught.

  So the model over-flags routine journal noise *and* under-flags real security events. F1 0.930 on BGL predicted neither, which is exactly what the `ARCHITECTURE.md` §9 domain-transfer risk said would happen. **12.2 is no longer conditional — it is required.**

- [x] 10.4b Group anomalies by template. A single mis-scored template became 4,091 findings, and the score cache is *why* — one wrong judgement is applied to every repeat for free. Correct for throughput, catastrophic for an inbox, and it also broke the retention sizing in `design.md` D7: 300k rows/year assumed a low flag rate, and 21% of all traffic is not that.

  Repeats of one template, on one host, in one hour now collapse to one row carrying `occurrences`, the first line as an exemplar, and the peak score (`GREATEST`, not the most recent — a group summarised by whichever line arrived last would hide its own worst case). Implemented as a partial unique index so only grouped rows are constrained.

  **Records that bypassed the cache are never grouped.** They bypassed because their content is security-relevant, and collapsing twenty failed logins into "occurrences: 20" throws away the twenty source addresses — which is the entire content of the finding. Verified: 20 brute-force lines from 20 addresses produce 20 rows with all 20 addresses intact.

  Measured on the same 19,836 journal lines:

  | | Before | After |
  |---|---|---|
  | Anomalies detected | 17,301 | 17,301 |
  | Rows in Postgres | 17,489 | **770** |
  | of which grouped / individual | — | 400 / 370 |

  Top row is now `Can't update stage views actor <*>` with `occurrences 5422`, and the GPU template that caused this is one row at 4,091. The in-process ratio reads 10.2× and Postgres shows 22.5×, because the hour-bucket upsert merges across batches as well as within them.

  **This does not fix the model.** 770 findings from 20k lines of one desktop is still unusable. It removes the volume amplification; 12.2 removes the cause
- [ ] 10.5 Record the observed peak line rate per host, so the 50–200 lines/s estimate stops being an estimate

## 11. Update `ARCHITECTURE.md`

- [x] 11.1 §1 — redraw the system diagram without Kafka; agents post to `ingest-service` — §1 redrawn — agents post to nginx → ingest-service → spool → collector
- [x] 11.2 §2.1 — remove Kafka from infrastructure services — §2.1 — Kafka removed, replaced by a note recording the measurement that killed it
- [x] 11.3 §2.2 — replace `log-ingestor` and `anomaly-detector` with `ingest-service` and `log-collector`; state the single-replica constraint and its reason — §2.2 — `ingest-service` and `log-collector` replace `log-ingestor`/`anomaly-detector`; single-replica constraint stated with its reason
- [x] 11.4 §3.1 — redraw the pipeline flow; the demo path becomes the offline test path, not a stand-in — §3.1 — agent path and offline path, both brokerless; replay.py reframed as CI test and load generator, not a stand-in
- [x] 11.5 §3.2 — delete the Kafka topic table; replace it with the ingest contract and the schema-version policy — §3.2 — topic table replaced by the transport section: wire contract, version policy, batch cap, durability boundary
- [x] 11.6 §7.1 — update the Compose topology table — §7.1 — Compose table with a built/planned column, plus the podman healthcheck and short-name findings
- [x] 11.7 §7.2 — expand the FluentBit section with buffering, TLS, tokens and the config template — §7.2 — buffering, TLS, tokens, config template, and the three field caveats (buffer size is a guess, Retry-After unread, PRIORITY is numeric)
- [x] 11.8 §8.1 — add `template_counts` and `agents` to the schema areas — §8.1 — `template_counts` and `agents` added
- [x] 11.9 §8.3 — **rewrite retention.** Anomalies plus hourly aggregates; state the discarded-normal-lines trade-off and the compliance fallback plainly — §8.3 — retention rewritten: costed table, what is lost stated plainly, irreversibility flagged, compliance fallback documented
- [x] 11.10 §9 — close "Streaming topology"; add the cache hit rate and the per-agent buffer size as open items; leave domain transfer open and updated with the task 10.3 number — §9 — streaming topology and throughput both closed; cache hit rate on HTTP logs, buffer size and the missing `modernbert-ft` checkpoint opened
- [x] 11.11 §10 — remove KRaft from the glossary; add ingest contract, template score cache, backpressure and drop policy — §10 — KRaft dropped; ingest contract, spool, template score cache, cache bypass, backpressure and drop policy added
- [x] 11.12 Update the last-updated header — header updated, and stale `log-ingestor`/`anomaly-detector` references swept from §3.3, §4.1 and §5.3

## 12. Follow-up changes to open (do not implement here)

- [ ] 12.1 ONNX + int8 export, if task 4 or task 10 shows the cache alone does not close the gap
- [x] 12.2a Corpus built from what is on disk — `ml/data/prepare_local.py`. Reuses `prepare.py`'s split, manifest and hashing rather than duplicating them; only the readers are new. Three sources in the final v3 corpus, 366,735 records:

  | source | records | anomaly rate | licence |
  |---|---|---|---|
  | nginx (zanbil.ir) | 163,156 | 0.74% | CC0 ✔ verified |
  | winlog (evtx-baseline + EVTX-to-MITRE-Attack) | 25,328 | 18.58% | Apache-2.0 + CC0 ✔ verified |
  | weblogs (Zenodo Web Server Log Dataset, organization-x/y) | 178,251 | 7.33% (after capping, see below) | **MIT recorded but unverified — Zenodo unreachable at build time, resolving it is the user's call, explicitly not a blocker** |

  nginx attacks labelled by rule against the **decoded request path only** (user-agent and referer are attacker-controlled, matching them would flag any visitor who mentions `wp-login`). EVTX rendered as `EventID=N field=value ...` from `EventData` only, `Channel`/`Provider`/`Level` excluded as leakage, and further restricted to the 86 EventIDs present in both classes (77 attack-only IDs would have been a free answer — measured: EventID-alone scored precision 0.998 before this restriction, 0.667 after). weblogs labelled with the dataset's own expert-written ground-truth YAML rules; one field-reference form (`"code: 404"`, written for a log2timeline timeline) is translated to a real HTTP status check anchored to where Apache actually puts it — the same 5xx-vs-byte-count trap already fixed once in `services/collector/cache.py`. Client IPv4s masked everywhere before anything is written.

  **Two bugs caught by measurement, not assumed absent:**
  - *Split.* `chronological_split` on every corpus gave winlog `train 0.61%` / `test 25.56%` anomalies — two concatenated collections, not one timeline, so splitting by position stranded every Windows attack in the tail. weblogs has the same shape (org-x Oct 2025, org-y Dec 2023–Apr 2025, non-overlapping). Both now use a seeded, class-stratified split; nginx keeps the chronological one because it genuinely is one site's traffic in order. Guarded by an assert on this exact input shape.
  - *Category dominance.* 294,304 of 297,375 raw weblogs positives (99%) were one template, `Failed password for X from Y`, from a server under real sustained bruteforce. Capped by even-sampling any category past 10,000 — same principle as the score cache grouping repeats in production, applied to training data instead of live traffic.

  `finetune.py --data-dir` already accepted the output shape, so no training code changes were needed.

- [x] 12.2 Domain fine-tune on real logs, trained and evaluated. **Result: promising, not yet promoted.**

  Trained on a free Colab T4 (`ml/train/colab_train_and_eval.ipynb`) — full fine-tune, 2 epochs, fp16. Getting there needed several real fixes, now committed: the notebook was cloning `main` instead of this branch (nothing it depends on exists there yet); `transformers` needed an upper bound (`<5`, since 5.x removed `TrainingArguments.warmup_ratio` — also fixed in `pyproject.toml` and the detector `Dockerfile`, which would have broken the next production image build the same way); `torch.cuda.is_bf16_supported()` returns `True` on a T4 by counting *emulated* bf16 — the actual gate is compute capability ≥ 8 (Ampere), fixed in `finetune.py`; and `LogScorer` never moved the model off CPU, so the harness ran at the same ~29.6 records/s as production CPU even with an idle T4 underneath it — fixed with an optional `device` param defaulting to `"cpu"` (production, `services/detector`, is completely unaffected; only the harness auto-detects and uses CUDA).

  **Measured on the v3 test split (55,012 records, 2,712 anomalies), on GPU:**

  | | F1 | vs classical floor |
  |---|---|---|
  | modernbert-ft-v3 | **0.986** | level-keyword 0.610, template-count 0.129 |

  False positive rate on normals: 0.09% (47/52,300). Per-category recall breaks down cleanly across ~50 distinct attack categories including rare ones (sqli, xss at n=1–2) — not just the dominant bruteforce template — which is real evidence against the "it just memorized one repeated pattern" failure mode. Genuine soft spots with enough samples to trust: T1574 DLL side-loading (40%, n=5), T1202 Indirect Command Execution (70%, n=27), nginx path traversal (80%, n=10), T1562.001 Impair Defenses (84%, n=19).

  **What this does not prove**, because it cannot: whether the model generalizes past the specific attack phrasing the labelling rules cover. Both label sources are rule/substring-derived; a model that recognizes the rules well is not the same claim as one that recognizes novel attacks. Only pilot traffic answers that.

  **Left open on purpose ("lo vemos otro día"), not forgotten:**
  - `smoke_score.py`'s 8-line probe was never run against v3 — the concrete check for whether `Accepted publickey` (v2's false positive, scored 0.81) is fixed
  - `modernbert-ft-v2` was never evaluated by the harness against any held-out split — only its smoke test and live pipeline flag-rates exist. A same-split harness comparison (`ml/eval/harness.py --checkpoint .../modernbert-ft-v2`) is needed before claiming v3 beats v2, not just beats the classical floor
  - the checkpoint (7 root files, **not** the `checkpoints/` subfolder — that's optimizer state from intermediate epochs, ~1.8GB each, never needed for inference) has not been downloaded from Drive into `models/cache/modernbert-ft-v3/`, registered in `ml/models.yaml`, or run through `ml/promote.py`. **Nothing is promoted. The registry default is still `modernbert-ft`, and the deployed stack still runs `baseline-bert` per the CI pin**

  **Scope was widened mid-flight: Linux *and* Windows**, not Windows-as-a-possibility as `ARCHITECTURE.md` §9 previously had it. The EVTX-to-text decision got made as a side effect of building the corpus (`EventID=N field=value ...`, identity masked) rather than as its own upfront design step — recorded here so it's visible that the decision exists and where it lives, not proposed as a separate open question anymore
- [ ] 12.2b Run `ml/smoke_score.py` against `modernbert-ft-v3` and `ml/eval/harness.py --checkpoint .../modernbert-ft-v2` on the same v3 test split, in the still-live Colab session if it survives, otherwise after downloading both checkpoints locally. Answers: is v2's `Accepted publickey` false positive actually fixed, and does v3 beat v2 on the same data, not just the classical floor
- [ ] 12.2c Download the v3 checkpoint's 7 root files (not `checkpoints/`) into `models/cache/modernbert-ft-v3/`, add the registry entry to `ml/models.yaml`, run `ml/promote.py --candidate modernbert-ft-v3`. **If the gate fails it is not promoted** — report the number either way. Blocked on nothing except doing it; the eval numbers already justify trying
  - Both now run on Colab via `ml/train/colab_promote.ipynb` (the dev machine can't score the split in reasonable time). The v2 checkpoint is lost, so v2 can never be compared on the v3 split; the incumbent is `modernbert-ft` (v1), whose checkpoint must be uploaded to `MyDrive/modernbert-ft/`. `modernbert-ft-v3` is registered in `ml/models.yaml`. `harness.py` now measures latency on CPU even when scoring on GPU (`--latency-device`), so the gate's budget is checked against detector-service hardware, not a T4. The gate runs as a dry run there; `--apply` happens in the repo once the result JSONs are committed
- [ ] 12.3 Windowed / session-level detection, still deferred, still the reason ModernBERT's 8k context was chosen
- [ ] 12.4 `api` service and SSE push, now unblocked by having a defined event path
- [ ] 12.5 Reintroduce a broker (Redpanda) only if a concrete second consumer or a replay requirement appears — with the requirement named, not anticipated
