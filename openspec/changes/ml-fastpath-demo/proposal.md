## Why

`ARCHITECTURE.md` commits the anomaly-detector to **LogBERT trained in-house**. That is the single biggest blocker to having anything runnable: it requires a labeled corpus, a training pipeline, GPU budget, and a research loop before a single anomaly is ever scored. The team has nothing running today, so all downstream work (RAG enrichment, alerting, dashboard, stream architecture) is stalled behind a model that does not exist.

The demo does not need a novel model. It needs **a score, on a real log line, today**, so the rest of the pipeline can be built, measured and iterated against. Off-the-shelf open-weight models on Hugging Face already do log anomaly classification well enough to prove the pipeline, and a small encoder can be fine-tuned on public log corpora (LogHub) in hours on commodity hardware once a baseline and an eval harness exist.

This change replaces "train LogBERT" with a **three-step fast path: baseline → evaluate → fine-tune**, and deliberately defers Kafka and streaming until the model layer is proven offline.

## What Changes

- **Drop LogBERT-from-scratch.** No custom pre-training, no masked-log-modeling research loop. Removed from the architecture as the detector's mechanism.
- **Adopt a fine-tuned open-weight HF encoder** as the detector. Day-0 baseline is an off-the-shelf checkpoint pulled straight from the Hub; the target is our own fine-tune of `answerdotai/ModernBERT-base` on LogHub-derived data.
- **Introduce a model lifecycle capability** — obtain, pin, evaluate, fine-tune, export, and version models — so a model swap is a config change plus a benchmark run, not a rewrite.
- **Introduce `detector-service`**, an HTTP scoring service (FastAPI + transformers, CPU inference) with a stable request/response contract. Everything downstream talks to that contract, not to a model.
- **Keep Drain3 as the parser, unchanged in mechanism, reduced in role.** Drain3 needs zero training and already works offline; it stays for template ID / parameter extraction and stays *out* of the critical path for scoring (the detector scores the raw message plus template context). Nothing about the parser needs to be refactored to get the demo running — the LLM log-parsing option (`B4lt/gemma4-loghub-e2b-GGUF`) is recorded as a future alternative, not adopted now.
- **Defer Kafka for the demo path.** Phase 0/1 replays log files from disk through a CLI harness. The Kafka topology in `ARCHITECTURE.md` remains the target end state and is planned in a follow-up change once the detector's real latency and batch behavior are measured.
- **Add an evaluation harness first.** Precision / recall / F1 / p95 latency on a held-out labeled split, reported per candidate model, so "our fine-tune beats the baseline" is a number and not a feeling.

### Candidate models researched (Hugging Face)

| Repo | Role | Why |
|---|---|---|
| [`AungMoonLord/bert-log-anomaly-detection`](https://hf.co/AungMoonLord/bert-log-anomaly-detection) | Day-0 baseline detector | BERT-base fine-tune for log anomaly text-classification, apache-2.0, 109M params, CPU-friendly, ~432K downloads. Zero training to run. |
| [`answerdotai/ModernBERT-base`](https://hf.co/answerdotai/ModernBERT-base) | Fine-tune base | apache-2.0, 8k context (whole log windows fit), faster than BERT at equal size, actively maintained. |
| [`Dumi2025/log-anomaly-detection-model-roberta`](https://hf.co/Dumi2025/log-anomaly-detection-model-roberta) | Comparison baseline | RoBERTa-base log-anomaly fine-tune, MIT. Second data point so the baseline is not a single checkpoint. |
| [`nomic-ai/modernbert-embed-base`](https://hf.co/nomic-ai/modernbert-embed-base) | RAG embeddings (resolves an existing TBD) | 768-dim, apache-2.0, pairs with the pgvector store already specified. |
| [`B4lt/gemma4-loghub-e2b-GGUF`](https://hf.co/B4lt/gemma4-loghub-e2b-GGUF) | Deferred alternative | Gemma-4 E2B fine-tuned on LogHub for log parsing/analysis, GGUF → runs on the Ollama service we already have. Recorded, not adopted. |
| [`bolu61/loghub_2`](https://hf.co/datasets/bolu61/loghub_2) | Training/eval corpus | LogHub 2.0, 38.4M lines, parquet, streams via `datasets`. Unlabeled `text` column — labels are derived from the original LogHub BGL/Thunderbird/HDFS label files. |
| [`B4lt/gemma4-loghub-sft-dataset`](https://hf.co/datasets/B4lt/gemma4-loghub-sft-dataset) | Reference SFT format | 80K chat-format LogHub rows with severity/component metadata; useful as a labeling and prompt-format reference. |

## Capabilities

### New Capabilities
- `ml-model-lifecycle`: How models are chosen, pinned, cached, fine-tuned, evaluated, exported and versioned. Covers the baseline → eval → fine-tune sequence, the LogHub dataset pipeline, and the promotion gate a model must pass before it becomes the default.
- `anomaly-detector-service`: The HTTP scoring contract and runtime for the detector — request/response schema, batching, CPU inference, model loading from a pinned local cache, health/readiness, and a file-replay harness that stands in for Kafka during the demo phase.

### Modified Capabilities
- `data-pipeline`: The anomaly detection stage no longer specifies LogBERT trained in-house. It specifies a fine-tuned open-weight transformer classifier served behind `detector-service`, and documents that the demo path replays files from disk while Kafka remains the target topology.

## Impact

- **Files**: `ARCHITECTURE.md` (§2.2, §3.4, §4.4, §8.1, §9 TBD table, glossary), new `services/detector/`, `ml/` (dataset prep, training, eval), `docker-compose.yml`
- **Dependencies**: `transformers`, `torch` (CPU), `datasets`, `scikit-learn`, `fastapi`, `uvicorn`, `huggingface_hub`; optional `peft`, `onnxruntime`, `optimum` for the export step
- **Systems**: Kafka, `log-ingestor`, `api`, `rag-engine` are untouched in this change. `anomaly-detector` is redefined as a client of `detector-service`
- **Hardware**: fine-tune targets a single consumer GPU or a free Colab T4; inference stays CPU-only, matching the PyME deployment constraint
- **Risk**: public-log-corpus fine-tunes may transfer poorly to real PyME logs. Mitigated by the eval harness and by keeping the baseline checkpoint swappable via config
- **Team**: unblocks pipeline, RAG and dashboard work immediately — they build against the `detector-service` contract, not against a model
