## 1. Phase 0 — Smoke: a real score today

- [x] 1.1 Create `ml/` and `services/detector/` project layout with a `pyproject.toml` managed by `uv` (matches the Distrobox dev env in `ARCHITECTURE.md` §7.3)
- [x] 1.2 Add dependencies: `transformers`, `torch` (CPU wheels), `huggingface_hub`, `fastapi`, `uvicorn`, `pydantic`, `drain3`
- [x] 1.3 Write `ml/download_models.py` — pull `AungMoonLord/bert-log-anomaly-detection` and `Dumi2025/log-anomaly-detection-model-roberta` into a local cache dir, recording the resolved revision SHA and license for each
- [x] 1.4 Write `ml/smoke_score.py` — load the baseline checkpoint, score a handful of hand-written log lines (a normal nginx line, a failed-ssh-auth burst, a kernel panic), print scores. **This is the first real signal; stop and eyeball it before continuing**
- [x] 1.5 Record the resolved model pins in `ml/models.yaml` (repo ID, revision SHA, license, role)

## 2. Phase 0 — `detector-service` skeleton

- [x] 2.1 Define the request/response Pydantic models: record (`message` required; `template_id`, `template`, `params`, `host`, `timestamp` optional) and result (`score`, `is_anomaly`)
- [x] 2.2 Implement `POST /score` with batch input, order-preserving output, and a configurable max batch size returning a client error when exceeded
- [x] 2.3 Implement validation errors that identify the offending record index
- [x] 2.4 Implement `GET /model` returning repo ID, pinned revision, active threshold, and stored eval metrics
- [x] 2.5 Implement `GET /healthz` (liveness) and `GET /readyz` (ready only once the model is loaded); scoring fails fast while not ready
- [x] 2.6 Fail loudly at startup when the configured model is missing from the cache and Hub access is disabled — error names repo and revision
- [x] 2.7 Make model ID, revision, threshold, max batch size, and inference thread count configurable via environment variables
- [x] 2.8 Verify end to end: `curl` a batch of log lines, get scores back

## 3. Phase 1 — Dataset pipeline

- [x] 3.1 Write `ml/data/prepare.py` — pull LogHub BGL and Thunderbird, streaming from [`bolu61/loghub_2`](https://hf.co/datasets/bolu61/loghub_2) where usable, falling back to the original LogHub release for the label field
- [x] 3.2 Derive per-line labels: alert field `-` → normal, any other value → anomaly; retain the original alert category as a secondary column for later analysis
- [x] 3.3 Generate a **chronological** train/validation/test split (test held out from the tail, not sampled at random)
- [x] 3.4 Emit a dataset manifest: corpus source + revision, row counts per split, class balance, and a content hash
- [x] 3.5 Verify determinism — run the script twice, confirm byte-identical splits
- [x] 3.6 Document the HDFS block-level labels as deferred to the windowed-detection phase, with a note on why they are not usable for per-line training

## 4. Phase 1 — Evaluation harness

- [x] 4.1 Write `ml/eval/harness.py` — take a model ID + revision (or a local checkpoint path), score the fixed test split, compute precision, recall, F1, PR-AUC
- [x] 4.2 Measure p95 single-record latency and p95 batch-of-32 latency on CPU; record on-disk model size
- [x] 4.3 Implement the classical floor: Drain3 template counts + IsolationForest (or PCA residual), evaluated through the same harness
- [x] 4.4 Write results to a versioned `ml/eval/results/*.json` recording model ID, revision, dataset manifest hash, and all metrics
- [x] 4.5 Run the harness against: classical floor, `AungMoonLord/bert-log-anomaly-detection`, `Dumi2025/log-anomaly-detection-model-roberta`
- [x] 4.6 Write up the comparison table. **Decision point: if the classical floor wins, say so out loud and reconsider the transformer path before spending GPU time**

  Comparison (full 43,419-record test split, actual anomaly rate 6.6%):

  | Model | F1 | Precision | Recall | PR-AUC | Flag rate |
  |---|---|---|---|---|---|
  | `level-keyword` (regex floor) | 0.253 | 0.148 | 0.883 | 0.138 | 39.7% |
  | `baseline-bert` (undocumented checkpoint) | 0.192 | 0.107 | 0.925 | 0.108 | 57.2% |
  | `template-count` (Drain3 + IsolationForest) | 0.153 | 0.083 | 0.996 | 0.080 | 79.6% |

  The classical floor wins. `baseline-bert` flags 57% of lines and its PR-AUC
  (0.108) is barely above the base rate — almost no ranking signal.
  `ml/promote.py --candidate baseline-bert` returns `GATE: REJECTED`.

  **Decision (2026-09-05, user confirmed): proceed with the fine-tune (Phase
  2) rather than reframe or stop.** Rationale: `baseline-bert` is an
  undocumented, unrelated checkpoint with no model card — its failure says
  nothing about whether a fine-tune on our own BGL split will fail the same
  way, which was always the premise of this change.

## 5. Phase 2 — Fine-tune

> **Environment note (updated 2026-09-05).** Original implementation machine
> had no GPU and no container runtime; those tasks were left unchecked rather
> than faked. Continuing now on a different device: NVIDIA GTX 1060 **3GB**,
> driver 580.173.02, CUDA 13.0, confirmed via `torch.cuda.is_available()` →
> `True`. 3GB VRAM is tight for a full `ModernBERT-base` fine-tune — expect
> task 5.3's LoRA fallback to trigger, not just exist as a contingency.

- [x] 5.1 Write `ml/train/finetune.py` — full fine-tune of `answerdotai/ModernBERT-base` for binary sequence classification on the labeled split
- [x] 5.2 Handle class imbalance (weighted loss or balanced sampling — BGL/Thunderbird anomaly rates are low); record the choice in the run metadata
- [ ] 5.3 Confirm the run fits a single consumer GPU / free-tier T4; if VRAM binds, fall back to LoRA via `peft` and note the switch — code supports `--lora`, needs an actual run on the GTX 1060 3GB to confirm which path fits
- [x] 5.4 Emit checkpoint plus run metadata: base model ID + revision, hyperparameters, dataset manifest hash, train/val curves — implemented, unverified until 5.3's run produces one
- [ ] 5.5 Evaluate the fine-tuned checkpoint through the same harness and add it to the comparison table — `ml/eval/harness.py --checkpoint` already supports this, needs a real checkpoint to run against
- [ ] 5.6 Apply the promotion gate: higher F1 than the incumbent with p95 latency inside budget. Promote by config only, or explicitly record why it was not promoted — `ml/promote.py --candidate` already implements this, needs a real eval result to gate on
- [ ] 5.7 Publish or archive the winning checkpoint into the model cache and pin it in `ml/models.yaml` — `ml/promote.py --apply` already does the pin, blocked on 5.6

## 6. Phase 3 — Pipeline integration

- [x] 6.1 Implement the Drain3 parsing step producing the detector input record (raw message + `template_id` + template + params + host + timestamp)
- [x] 6.2 Write the replay harness CLI: log file → Drain3 parse → batched `POST /score` → scored output, reporting throughput and observed latency
- [x] 6.3 Bundle a small sample log file and wire the harness as a CI smoke test that runs with no network access — `.github/workflows/ci.yml` runs `tools/replay.py --assert-anomalies 1` offline against `data/samples/HDFS_2k.log`
- [x] 6.4 Persist scored anomalies to Postgres (`anomalies` table per `ARCHITECTURE.md` §8.1), storing raw score, threshold, model ID and revision alongside each row
- [x] 6.5 Add `detector-service` to `docker-compose.yml` — internal-only on the shared network, port 8003, model cache mounted as a volume, `HF_HUB_OFFLINE=1`
- [ ] 6.6 Verify the full demo path inside Compose: file in, scored anomalies in Postgres

## 7. Optional — Latency escape hatch

- [ ] 7.1 Only if the p95 latency from task 4.2 / 5.5 misses budget: export the promoted checkpoint to ONNX with int8 quantization via `optimum`
- [ ] 7.2 Re-run the harness on the quantized export and record the accuracy/latency trade-off in the same results table
- [ ] 7.3 Promote the quantized export only if it clears the same gate

## 8. Update `ARCHITECTURE.md`

- [x] 8.1 §2.2 service catalog — anomaly-detector described as a client of `detector-service`; add `detector-service` (Python/FastAPI + transformers, port 8003, CPU)
- [x] 8.2 §3.4 — replace the LogBERT section with the fine-tuned open-weight classifier: base model, corpus, pinned revision, threshold, CPU inference
- [x] 8.3 §3.1 — add the demo-phase flow diagram (file → replay → Drain3 → `/score` → Postgres) and mark the Kafka topology as the target end state, deferred
- [x] 8.4 §4.4 / §8.2 — record `nomic-ai/modernbert-embed-base` as the RAG embedding model and fix the pgvector column at 768 dimensions
- [x] 8.5 §8.1 — extend the anomalies schema area with model ID, revision, raw score, and threshold columns
- [x] 8.6 §9 TBD table — close "LogBERT training infrastructure" and "Embedding model for RAG"; restate "Anomaly detection threshold" as a configured value pending pilot validation
- [x] 8.7 §7.1 — add `detector-service` to the Compose topology table
- [x] 8.8 §10 glossary — demote LogBERT to prior art with a note on why it was rejected; add ModernBERT, LogHub, and the promotion gate
- [x] 8.9 Update the "Date of last update" header

## 9. Follow-up changes to open (do not implement here)

- [ ] 9.1 Kafka streaming topology — sized using the real batch size and latency numbers from tasks 4.2 / 6.2
- [ ] 9.2 Windowed / session-level detection using ModernBERT's 8k context and the deferred HDFS block labels
- [ ] 9.3 Domain fine-tune on real PyME logs (nginx / sshd / systemd / Windows Event Log) once pilot data exists
