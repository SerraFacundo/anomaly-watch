# ML — model lifecycle

Obtain, evaluate, fine-tune, promote. Every model the stack serves goes through
this path, and a model swap is meant to cost one config value plus a benchmark
run.

## Quick start

```bash
uv sync --extra detector --extra ml
uv run python ml/download_models.py --model baseline-bert
uv run python ml/smoke_score.py
```

Then serve it:

```bash
uv run uvicorn services.detector.app:app --port 8003
```

## The pipeline

| Step | Command | Produces |
|---|---|---|
| Pin & fetch | `python ml/download_models.py` | Weights in `models/cache/`, revision SHAs written into `models.yaml` |
| Build dataset | `python ml/data/prepare.py` | `ml/data/build/{train,validation,test}.jsonl` + `manifest.json` |
| Evaluate | `python ml/eval/harness.py --all` | `ml/eval/results/<name>.json` |
| Fine-tune | `python ml/train/finetune.py` | `ml/train/runs/modernbert-ft/` + run metadata |
| Promote | `python ml/promote.py --candidate modernbert-ft --apply` | Updated `default:` in `models.yaml` |

## Rules

- **Revisions are commit SHAs, never branches.** A moved branch means the model
  you evaluated is not the model you deployed.
- **The deployed stack is offline** (`HF_HUB_OFFLINE=1`). No runtime downloads, so
  log data cannot egress and served weights are exactly the evaluated weights.
- **Permissive licenses only** (apache-2.0 / MIT). `download_models.py` refuses
  anything else — the stack is redistributed to self-hosting customers.
- **Chronological splits.** A random split lets a burst's start land in train and
  its tail in test, inflating every metric.
- **The promotion gate refuses cross-dataset comparisons.** Results carry their
  dataset manifest hash and whether the run covered the full split.

## Two traps, both hit for real

### Label-index inversion

The phase-0 baseline checkpoint (`AungMoonLord/bert-log-anomaly-detection`) ships
no `id2label`. Assuming the usual "index 1 is the positive class" inverts every
score: it scored **0.975** on a routine `systemd` line and **0.015** on a kernel
panic — 0/8 on the smoke probes. With the index corrected it scores 8/8.

The correct index lives in `models.yaml` as `anomaly_index` and is reported by
`GET /model`. Our own fine-tunes write `id2label` into `config.json` so the
ambiguity cannot recur.

**If a model scores near-perfectly inverted, check this before anything else.**

### Label leakage from the severity field

BGL lines carry a `Level` field that is very nearly the label itself:

| Level | Normal | Anomaly |
|---|---:|---:|
| `INFO` | 220,658 | 0 |
| `FATAL` | 10,070 | 169,185 |

Scoring text containing it would yield a near-perfect F1 that reflects reading
one word. Only the `Content` field becomes `message`; `Level`, `Component` and
`Node` stay in metadata.

The `level-keyword` baseline runs on every evaluation so the shortcut stays
visible. With `Level` correctly excluded it scores F1 ≈ 0.25 — BGL normals
contain words like "error" too (`instruction cache parity error corrected` is a
*normal* line). If that number ever jumps toward 1.0, leakage has crept back in.

## Dataset choices

**BGL only, by default.** Measured, not assumed:

- **BGL** — 4.6M lines, ~7.2% anomaly rate, real alert categories. Good.
- **Thunderbird** — 44 labeled anomalies across 5.38M lines (0.0008%). Adds
  normals and no signal, and drags the test-set base rate from 7.2% to 3.3%.
  Excluded by default; opt in with `--corpus thunderbird`.
- **HDFS** — labels are per `block_id` in `anomaly_label.csv`, i.e.
  **session-level, not per-line**. Unusable for the current per-line framing.
  Deferred to the windowed-detection phase, where it is the natural fit.

**Strided sampling, not head slicing.** BGL anomalies arrive in dense bursts, so
the first 60MB is 42% anomalies against a true rate of ~7%. Taking every Nth line
across the whole file preserves base rate and chronology and stays deterministic.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DETECTOR_MODEL_KEY` | `models.yaml` `default:` | Which registry entry to serve |
| `DETECTOR_MODEL_REVISION` | registry pin | Override the pinned revision |
| `DETECTOR_THRESHOLD` | `0.5` | Score-to-flag cutoff |
| `DETECTOR_MAX_BATCH_SIZE` | `256` | Larger batches are rejected, not queued |
| `DETECTOR_MAX_LENGTH` | `256` | Tokenizer truncation length |
| `DETECTOR_NUM_THREADS` | unset | Bound inference threads so co-located services are not starved |
| `DETECTOR_ANOMALY_INDEX` | registry / config | Last-resort label-index override |
| `ANOMALY_WATCH_MODEL_CACHE` | `models/cache` | Weight cache location |
| `HF_HUB_OFFLINE` | `1` in the container | Disable runtime Hub access |
