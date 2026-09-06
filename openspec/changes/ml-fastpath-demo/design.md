## Context

Anomaly Watch has an architecture document and no code. `ARCHITECTURE.md` §3.4 specifies LogBERT with "training infrastructure: TBD", which is the load-bearing unknown for the whole system: nothing downstream can be exercised until something produces an anomaly score.

The goal of this change is explicitly **time-to-first-score**, not detection quality. Quality is a later optimization, made possible by having an eval harness and a swappable model from day one.

**Current state**: no services, no models, no data. `ARCHITECTURE.md` describes an aspirational Kafka-based topology.
**Target state after this change**: a `detector-service` returning real scores for real log lines, a reproducible eval number for at least two candidate models, and a fine-tuned checkpoint of our own that beats or matches the off-the-shelf baseline.

**What stays true from `ARCHITECTURE.md`**: self-hosted Docker Compose, CPU-only inference, data never leaves the PyME network, Drain3 parsing, Postgres + pgvector, RAG over MITRE ATT&CK, SSE alerts. Only the model layer and the demo-phase transport change.

### Constraints
- Inference must run on CPU on commodity PyME hardware (no GPU at the customer site)
- Training must fit a single consumer GPU or a free Colab T4 session
- Model weights must be permissively licensed (apache-2.0 / MIT) for self-hosted redistribution
- No runtime downloads from the Hub in a deployed stack — weights ship pre-cached in a volume

## Goals / Non-Goals

**Goals:**
- A running scoring endpoint within the first phase, using zero-training off-the-shelf weights
- A repeatable eval harness that produces precision / recall / F1 / p95 latency per model on a fixed split
- A fine-tuned encoder of our own, trained on LogHub-derived data, beating the baseline on that harness
- A model swap that costs one config value and one benchmark run
- Resolve two standing TBDs from `ARCHITECTURE.md` §9: detection model and RAG embedding model

**Non-Goals:**
- Training LogBERT, or any pre-training from scratch
- Kafka wiring, consumer groups, backpressure, or stream semantics — a separate follow-up change
- Sequence-level / session-window detection (HDFS block-level grouping) — phase 1 is per-line
- The RAG engine, alerting, notifier, dashboard, or auth
- Production hardening: autoscaling, GPU serving, model A/B routing
- Detection accuracy targets on real PyME logs (no such logs exist yet)

## Decisions

### D1: Drop LogBERT-from-scratch, fine-tune an open-weight encoder

**Choice**: Detection is a fine-tuned open-weight transformer text classifier. `answerdotai/ModernBERT-base` is the fine-tune target; `AungMoonLord/bert-log-anomaly-detection` is the day-0 baseline.

**Rationale**: LogBERT's value is masked-log-modeling on unlabeled data — a research contribution, not a demo requirement. A supervised classifier on labeled LogHub data reaches a usable signal in hours instead of weeks. ModernBERT-base is apache-2.0, ~150M params, has an 8k context (so a whole log window fits in one forward pass, useful when we move from per-line to windowed detection), and is materially faster than BERT-base at equal quality.

**Alternatives considered**:
- *Train LogBERT as originally planned* → weeks of work, GPU budget, blocks everything. Rejected as the entire point of this change.
- *Generative LLM classifier via Ollama (`B4lt/gemma4-loghub-e2b-GGUF`)* → the Ollama service already exists, and the model is LogHub-tuned. But generation is 10–100× slower per line than an encoder, output needs parsing, and there is no clean probability score for thresholding. Recorded as a future option for explanation, not detection.
- *Classical baseline (IsolationForest / PCA over Drain3 template counts)* → faster still and genuinely competitive on HDFS. Kept as the **floor** in the eval harness: if a transformer cannot beat template-count anomaly detection, that is a finding worth having early.

**Reversibility**: high. The `detector-service` contract hides the model entirely; LogBERT can be reintroduced later as one more implementation behind it.

### D2: Keep Drain3, do not refactor the parser

**Choice**: `log-ingestor` keeps Drain3 exactly as specified. No LLM parser, no learned parser.

**Rationale**: Drain3 requires zero training, runs online, is CPU-cheap and is already the fastest possible option. The parser was never the bottleneck — the detector was. Refactoring it now would add work to a change whose stated purpose is removing work.

**Consequence**: the detector consumes the **raw message plus template metadata** (`template_id`, `template`, `params`), not template IDs alone. Raw text is what the HF checkpoints were trained on, so feeding template IDs would throw away the pretrained model's advantage. Template metadata still travels with the record for grouping, the classical baseline, and RAG context.

### D3: Per-line binary classification for phase 1

**Choice**: input is one log line (plus optional template context), output is `{score: float, label: normal|anomaly}`.

**Rationale**: matches what the off-the-shelf checkpoints already do, makes labeling straightforward, and makes the baseline and our fine-tune directly comparable. Window/session-level detection (the thing LogBERT and HDFS block-labels actually target) is strictly better for real detection quality but multiplies dataset-prep work.

**Deferred**: windowed detection is a follow-up. ModernBERT's 8k context is chosen now specifically so that upgrade needs no base-model change.

### D4: Dataset — LogHub, labels derived from the original release

**Choice**: corpus is [`bolu61/loghub_2`](https://hf.co/datasets/bolu61/loghub_2) (LogHub 2.0, 38.4M lines, parquet, streamable). Labels are derived from the original LogHub release, not from that upload.

**Rationale**: the HF upload exposes a single `text` column with no labels. The labeled subsets we need are:
- **BGL / Thunderbird** — each line is prefixed with an alert label; `-` means normal, anything else is a labeled alert category. Per-line labels, which is exactly D3's framing.
- **HDFS** — labels are per `block_id` in `anomaly_label.csv`, i.e. session-level. Held back for the windowed phase.

So phase 1 trains and evaluates on **BGL + Thunderbird per-line labels**, with a stratified, *chronologically* held-out split.

**Alternatives considered**:
- *Synthesize labels with an LLM* → cheap to start, unverifiable, and would make our eval number meaningless. Rejected.
- *[`B4lt/gemma4-loghub-sft-dataset`](https://hf.co/datasets/B4lt/gemma4-loghub-sft-dataset)* → chat-format, task-mixed, built for generative SFT, carries `severity`/`component` metadata. Used as a **formatting and metadata reference**, not as the training set for a classifier.

**Risk**: BGL/Thunderbird are HPC supercomputer logs. A PyME runs nginx, sshd, systemd and Windows Event Log. Transfer will be imperfect — this is the accepted cost of having anything at all, and it is why D6's promotion gate exists and why the baseline stays swappable.

### D5: Serving — FastAPI + transformers on CPU, model behind a stable contract

**Choice**: a new `detector-service` (FastAPI, port 8003) exposing `POST /score` (batch), `GET /healthz`, `GET /model` (returns active model ID + pinned revision + eval metrics). `anomaly-detector` becomes a thin client of this contract.

**Rationale**: separating "the model" from "the pipeline" is the whole reason a swap stays cheap. It also lets the model be exercised by `curl` and by the eval harness without Kafka, a broker, or any pipeline code existing.

**Details**:
- Batch scoring, since transformer throughput on CPU is dominated by batch size
- Model resolved from a local cache directory mounted as a Docker volume; `HF_HUB_OFFLINE=1` in the deployed stack so no runtime egress can occur
- Model ID and revision SHA are config, never hardcoded
- ONNX + int8 quantization via `optimum` is an **optional** phase-3 step, gated on the p95 latency number actually being a problem

### D6: Eval harness before fine-tuning, and a promotion gate

**Choice**: the eval harness is built and run against the baselines *before* any fine-tuning starts. A model becomes the configured default only if it beats the incumbent on F1 without regressing p95 latency past the budget.

**Rationale**: without a number, "the fine-tune is better" is unfalsifiable and every future model decision becomes an argument. Building the harness first also costs almost nothing — it is a fixed split, four metrics, and a table.

**Reported per model**: precision, recall, F1, PR-AUC, p95 single-line latency, p95 batch-of-32 latency, model size on disk. Contenders: classical template-count floor, `AungMoonLord/bert-log-anomaly-detection`, `Dumi2025/log-anomaly-detection-model-roberta`, our ModernBERT fine-tune.

### D7: Full fine-tune, not LoRA

**Choice**: full fine-tune of ModernBERT-base. `peft`/LoRA is a fallback if VRAM turns out to be the constraint.

**Rationale**: at ~150M params a full fine-tune fits comfortably on a T4 and produces a single self-contained checkpoint with no adapter-merge step at inference. LoRA's advantages (cheap multi-task adapters, tiny artifacts) buy us nothing at this size.

### D8: Defer Kafka; replay files from disk

**Choice**: phase 0–2 use a CLI replay harness — read a log file, parse with Drain3, POST batches to `detector-service`, write results to Postgres or stdout. Kafka stays in `ARCHITECTURE.md` as the target and gets its own change.

**Rationale**: Kafka adds a broker, topic config, consumer-group semantics, offset handling and failure modes to a phase whose only question is "does the model produce a useful score". Deferring it also means the streaming design gets made **after** we know the detector's real batch size and latency, which are precisely the inputs that should drive it.

### D9: Resolve the RAG embedding TBD now

**Choice**: [`nomic-ai/modernbert-embed-base`](https://hf.co/nomic-ai/modernbert-embed-base), 768-dim, apache-2.0.

**Rationale**: `ARCHITECTURE.md` §8.2 needs a fixed dimensionality before the pgvector column type can be written, and §9 lists it as TBD. Same ModernBERT family as the detector, so one architecture family and one tokenizer story across the stack. 768 dims is the pgvector column width.

**Note**: this is a low-cost decision recorded here to unblock schema work. The RAG engine itself is out of scope for this change.

## Risks / Trade-offs

| Risk | Mitigation |
|---|---|
| HPC-log fine-tune transfers poorly to PyME logs (nginx/sshd/Windows) | Accepted for the demo. Eval harness makes the gap visible; model is config-swappable; a real-log fine-tune is a follow-up once pilot data exists |
| Off-the-shelf baseline checkpoint is undocumented (no model card, unknown training data) | Used only as a phase-0 smoke test and a comparison point, never as the shipped default. Two independent baselines are evaluated, not one |
| CPU inference too slow at PyME log volumes | Measured explicitly in the harness (p95, batch-32). ONNX + int8 export is the pre-planned escape hatch. Real throughput requirements come out of the deferred Kafka design |
| Per-line detection produces excessive false positives at scale | Known limitation of D3. Windowed detection is the planned upgrade and the base model was chosen to support it without a rewrite |
| `ARCHITECTURE.md` drifts from what is actually built | This change updates `ARCHITECTURE.md` in the same tasks list, not afterwards |

## Migration Plan

No migration — nothing is deployed. Rollout is phased:

1. **Phase 0 — smoke.** Pull the baseline checkpoint, score log lines locally, stand up `detector-service`. Deliverable: a real score from a `curl`.
2. **Phase 1 — measure.** Build the LogHub dataset pipeline and eval harness. Deliverable: a metrics table for the classical floor and both off-the-shelf baselines.
3. **Phase 2 — fine-tune.** Train ModernBERT-base on BGL+Thunderbird. Deliverable: our checkpoint in the same table, promoted only if it clears the gate.
4. **Phase 3 — integrate.** Wire `detector-service` into Docker Compose, persist results, update `ARCHITECTURE.md`. Deliverable: file → parse → score → store, end to end.

**Rollback**: model config points back at the previous pinned revision. No data migration, no schema break.

## Open Questions

- What real PyME log sources will the pilot actually produce (nginx, sshd, systemd, Windows Event Log)? Determines whether a second fine-tune on domain-matched data is needed before pilot.
- What false-positive rate is tolerable for a PyME with no dedicated SOC? Sets the score threshold, currently an unvalidated config value.
- Does windowed detection need session grouping by host, by template, or by time? Input to the deferred streaming design.
- Is anomaly severity worth predicting as a second head (the LogHub SFT dataset carries `severity`), or does RAG enrichment cover it?
