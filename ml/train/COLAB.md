# Fine-tuning on a free Colab T4

The local GTX 1060 (3 GB) OOMs on a full fine-tune — measured, not assumed. A
free T4 (16 GB) fits it with room to spare.

Two things travel to Colab and they come from different places:

| | what | where from |
|---|---|---|
| code | `finetune.py`, `registry.py`, `models.yaml`, `download_models.py` | the repo, cloned |
| data | `ml/data/build-local/` | Drive — it is **gitignored and not in the repo** |

So even with a clone, the corpus still has to be uploaded. Raw sources never go:
they are 12 GB and Colab has no use for them.

## 1. GitHub — create the token

`Settings → Developer settings → Personal access tokens → Fine-grained tokens`

- Repository access: **Only select repositories** → `anomaly-watch`
- Permissions → Repository permissions → **Contents: Read-only**
- Expiration: the shortest that covers the run

It is shown once. Copy it.

## 2. Colab — store it as a Secret

Key icon in the left sidebar → `+ Add new secret`. Name `GH_TOKEN`, paste the
value, enable **Notebook access**.

Do not paste it into a cell. Cell contents are saved with the notebook and
travel with every copy of it.

## 3. Runtime → Change runtime type → **T4 GPU**

Before running anything. Changing it later wipes the VM.

## 4. Pack the corpus on this machine, upload to Drive

```bash
tar czf corpus.tgz -C ml/data build-local
```

Upload `corpus.tgz` to the root of *My Drive*. It is ~10 MB.

## 5. Cell 1 — mount and clone

```python
from google.colab import drive, userdata
import subprocess
drive.mount('/content/drive')

subprocess.run(['git', 'clone', '-q',
                f"https://{userdata.get('GH_TOKEN')}@github.com/SerraFacundo/anomaly-watch",
                '/content/anomaly-watch'], check=True)
print("clone ok")
```

`check=True` is the point. Without it a failed clone is silent and the error
surfaces two cells later as `No such file or directory`.

## 6. Cell 2 — corpus and dependencies

```python
!mkdir -p /content/data && tar xzf /content/drive/MyDrive/corpus.tgz -C /content/data
!pip -q install "transformers==4.57.6" "datasets>=3.2,<6" "accelerate>=1.2" scikit-learn pyyaml
!ls /content/data/build-local/
```

**The transformers pin is load-bearing.** 5.x reworked `TrainingArguments` and
removed `warmup_ratio`; an unbounded `>=4.48` installs it and `finetune.py` dies
after tokenising the whole corpus. 4.57.6 is the line that produced the shipped
checkpoint.

## 7. Cell 3 — confirm the GPU is attached

```python
import torch; print(torch.cuda.get_device_name(0))
```

Not `Tesla T4`? Stop. On CPU this is days, not hours.

## 8. Cell 4 — base model

```python
%cd /content/anomaly-watch
!python ml/download_models.py --model modernbert-base
```

The registry decides which model and pins it to a revision SHA, so this stays
auditable instead of hardcoded in a notebook.

## 9. Cell 5 — train

Short run first, to prove the pipeline before spending the session:

```python
!python ml/train/finetune.py --data-dir /content/data/build-local \
    --out /content/ft-smoke --epochs 1 --train-limit 20000
```

Then the real one:

```python
!python ml/train/finetune.py \
    --data-dir /content/data/build-local \
    --out /content/drive/MyDrive/modernbert-ft-v2 \
    --epochs 2 --batch-size 32
```

`--out` writes to Drive, so a dropped session does not lose the run. If VRAM
binds, `--lora` is the fallback and the run metadata records that it was used.

## 10. Revoke the token

GitHub, same page, `Delete`. It was handed to a third-party VM.

## 11. Back here — measure before believing

Download the checkpoint into `models/cache/modernbert-ft-v2/`, add it to
`ml/models.yaml` with `repo_id: modernbert-ft-v2` (a directory name, not a path —
`ml/scorer.py` resolves it against the cache), then:

```bash
uv run python ml/eval/harness.py --model modernbert-ft-v2 --data-dir ml/data/build-local
```

```bash
uv run python ml/promote.py --candidate modernbert-ft-v2
```

**If the gate fails it is not promoted.** Report the number either way.

## What to distrust in the result

Both label sources are shortcuts a model can learn instead of the task:

- nginx labels come from regexes over the same text the model reads. A high F1
  may mean it reproduced the rule set.
- Windows positives and negatives come from different collections. The obvious
  shortcuts are neutralised — identity fields masked, records restricted to
  EventIDs present in both classes, which took the EventID-only baseline from
  F1 0.643 to 0.006 and the hostname-only baseline from precision 1.000 to 0 —
  but absence of the shortcuts you looked for is not absence of shortcuts.

The rule: the harness reports a keyword baseline alongside. **A fine-tune that
beats the transformer baseline but not the keyword baseline has learned the
shortcut.** That is what happened to BGL's `Level` field.

Then run `ml/smoke_score.py` against the new checkpoint and read the eight lines
yourself. Eight lines you actually read are worth more than a three-decimal F1
on a test set we labelled ourselves.

Test-split precision is optimistic by construction: every nginx attack line is
kept while normals are strided, so the anomaly rate sits far above the ~0.02%
seen in the wild. The honest number needs pilot traffic.
