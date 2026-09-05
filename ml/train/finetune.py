"""Fine-tune an open-weight encoder for binary log anomaly classification.

Replaces the original plan to pre-train LogBERT from scratch. Default base is
answerdotai/ModernBERT-base: apache-2.0, ~150M params, and an 8k context so the
later move from per-line to windowed detection needs no base-model swap.

Sized for one consumer GPU or a free-tier T4. If VRAM binds, pass --lora.
Inference stays CPU-only regardless — a PyME site has no GPU.

Usage:
    python ml/train/finetune.py
    python ml/train/finetune.py --epochs 2 --batch-size 16 --lora
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "ml"))

from registry import DEFAULT_CACHE_DIR, get_pin  # noqa: E402

DATA_DIR = REPO_ROOT / "ml" / "data" / "build"
RUNS_DIR = REPO_ROOT / "ml" / "train" / "runs"

# The registry decides what we fine-tune from, so the base model is pinned and
# auditable like every other model in the stack.
BASE_MODEL_KEY = "modernbert-base"


def load_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}. Run: python ml/data/prepare.py")
    records = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            records.append(json.loads(line))
            if limit and len(records) >= limit:
                break
    return records


def build_dataset(records: list[dict], tokenizer, max_length: int):
    from datasets import Dataset

    dataset = Dataset.from_dict(
        {
            "text": [record["message"] for record in records],
            "labels": [record["label"] for record in records],
        }
    )
    return dataset.map(
        lambda batch: tokenizer(batch["text"], truncation=True, max_length=max_length),
        batched=True,
        remove_columns=["text"],
    )


def compute_metrics(eval_prediction):
    import numpy as np
    from sklearn.metrics import precision_recall_fscore_support

    logits, labels = eval_prediction
    predictions = np.argmax(logits, axis=-1)
    precision, recall, f1, _ = precision_recall_fscore_support(
        labels, predictions, average="binary", zero_division=0
    )
    return {"precision": precision, "recall": recall, "f1": f1}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=BASE_MODEL_KEY, help="registry key of the base model")
    parser.add_argument("--out", type=Path, default=RUNS_DIR / "modernbert-ft")
    parser.add_argument("--epochs", type=float, default=2.0)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--eval-batch-size", type=int, default=64)
    parser.add_argument("--lr", type=float, default=3e-5)
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--warmup-ratio", type=float, default=0.06)
    parser.add_argument("--weight-decay", type=float, default=0.01)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--train-limit", type=int, default=None)
    parser.add_argument("--eval-limit", type=int, default=20_000)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--lora", action="store_true", help="LoRA instead of a full fine-tune (VRAM fallback)")
    parser.add_argument(
        "--imbalance",
        choices=["class-weights", "none"],
        default="class-weights",
        help="BGL/Thunderbird anomaly rates are low; weighting the loss is the default",
    )
    args = parser.parse_args()

    import torch
    from transformers import (
        AutoModelForSequenceClassification,
        AutoTokenizer,
        DataCollatorWithPadding,
        Trainer,
        TrainingArguments,
    )

    pin = get_pin(args.base)
    revision = pin.require_revision()
    print(f"base model : {pin.repo_id} @ {revision}")

    load_kwargs = {
        "pretrained_model_name_or_path": pin.repo_id,
        "revision": revision,
        "cache_dir": str(args.cache_dir),
    }
    tokenizer = AutoTokenizer.from_pretrained(**load_kwargs)

    # ModernBERT only skips torch.compile for cpu/mps, not by CUDA capability —
    # it crashes on pre-Volta GPUs (e.g. GTX 1060, cc 6.1) where triton is
    # installed but unsupported. Leave it unset (library default) on capable
    # GPUs like a T4 (cc 7.5) so compile still speeds those up.
    reference_compile = None
    if torch.cuda.is_available() and torch.cuda.get_device_capability()[0] < 7:
        reference_compile = False

    model = AutoModelForSequenceClassification.from_pretrained(
        **load_kwargs,
        num_labels=2,
        # Written into config.json so downstream loaders never have to guess
        # which index means anomaly — the exact trap the baseline checkpoint set.
        id2label={0: "Normal", 1: "Anomaly"},
        label2id={"Normal": 0, "Anomaly": 1},
        reference_compile=reference_compile,
    )

    if args.lora:
        from peft import LoraConfig, TaskType, get_peft_model

        model = get_peft_model(
            model,
            # ModernBERT has no default target-module mapping in peft, so
            # "all-linear" is required — task_type alone isn't enough here.
            LoraConfig(
                task_type=TaskType.SEQ_CLS, r=16, lora_alpha=32, lora_dropout=0.05,
                target_modules="all-linear",
            ),
        )
        model.print_trainable_parameters()

    manifest_path = args.data_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}

    train_records = load_jsonl(args.data_dir / "train.jsonl", args.train_limit)
    eval_records = load_jsonl(args.data_dir / "validation.jsonl", args.eval_limit)
    print(f"train      : {len(train_records):,}  eval: {len(eval_records):,}")

    anomalies = sum(record["label"] for record in train_records)
    normals = len(train_records) - anomalies
    print(f"class balance: {anomalies:,} anomaly / {normals:,} normal "
          f"({anomalies / max(len(train_records), 1):.2%})")

    train_dataset = build_dataset(train_records, tokenizer, args.max_length)
    eval_dataset = build_dataset(eval_records, tokenizer, args.max_length)

    class_weights = None
    if args.imbalance == "class-weights" and anomalies and normals:
        total = anomalies + normals
        class_weights = torch.tensor(
            [total / (2 * normals), total / (2 * anomalies)], dtype=torch.float
        )
        print(f"class weights: {class_weights.tolist()}")

    class WeightedTrainer(Trainer):
        """Standard Trainer, except the loss is class-weighted.

        Without this the model can hit high accuracy by always predicting
        'normal', which is exactly the failure mode a low anomaly rate invites.
        """

        def compute_loss(self, model, inputs, return_outputs=False, **kwargs):
            labels = inputs.pop("labels")
            outputs = model(**inputs)
            weight = class_weights.to(outputs.logits.device) if class_weights is not None else None
            loss = torch.nn.functional.cross_entropy(outputs.logits, labels, weight=weight)
            return (loss, outputs) if return_outputs else loss

    args.out.mkdir(parents=True, exist_ok=True)
    training_arguments = TrainingArguments(
        output_dir=str(args.out / "checkpoints"),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.eval_batch_size,
        learning_rate=args.lr,
        warmup_ratio=args.warmup_ratio,
        weight_decay=args.weight_decay,
        seed=args.seed,
        eval_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model="f1",
        greater_is_better=True,
        logging_steps=50,
        save_total_limit=2,
        bf16=torch.cuda.is_available(),
        report_to=[],
    )

    trainer = (WeightedTrainer if class_weights is not None else Trainer)(
        model=model,
        args=training_arguments,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        data_collator=DataCollatorWithPadding(tokenizer),
        compute_metrics=compute_metrics,
    )

    trainer.train()
    trainer.save_model(str(args.out))
    tokenizer.save_pretrained(str(args.out))

    # Everything needed to explain, reproduce or distrust this checkpoint later.
    run_metadata = {
        "base_model": {"key": pin.key, "repo_id": pin.repo_id, "revision": revision},
        "dataset": {
            "manifest_sha256": manifest.get("manifest_sha256"),
            "dataset_revision": manifest.get("dataset_revision"),
            "train_records": len(train_records),
            "eval_records": len(eval_records),
            "train_anomaly_rate": round(anomalies / max(len(train_records), 1), 6),
        },
        "hyperparameters": {
            "epochs": args.epochs,
            "batch_size": args.batch_size,
            "learning_rate": args.lr,
            "max_length": args.max_length,
            "warmup_ratio": args.warmup_ratio,
            "weight_decay": args.weight_decay,
            "seed": args.seed,
            "imbalance_strategy": args.imbalance,
            "class_weights": class_weights.tolist() if class_weights is not None else None,
            "lora": args.lora,
        },
        "log_history": trainer.state.log_history,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "device": "cuda" if torch.cuda.is_available() else "cpu",
    }
    (args.out / "run_metadata.json").write_text(
        json.dumps(run_metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    print(f"\ncheckpoint: {args.out}")
    print(f"next: python ml/eval/harness.py --checkpoint {args.out} --name modernbert-ft")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
