"""Evaluate any candidate detector on the fixed test split and record the numbers.

Every candidate — off-the-shelf checkpoint, our fine-tune, or a classical
baseline — goes through this one entry point, on the same split, reporting the
same metrics. That is what makes "the fine-tune is better" a fact rather than an
opinion, and what the promotion gate in ml/promote.py reads.

Usage:
    python ml/eval/harness.py --model baseline-bert
    python ml/eval/harness.py --checkpoint ml/train/runs/modernbert-ft
    python ml/eval/harness.py --baseline template-count
    python ml/eval/harness.py --all
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "ml"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from baselines import level_keyword_scores, template_count_scores  # noqa: E402
from registry import DEFAULT_CACHE_DIR, ModelPin, get_pin, iter_pins  # noqa: E402

DATA_DIR = REPO_ROOT / "ml" / "data" / "build"
RESULTS_DIR = REPO_ROOT / "ml" / "eval" / "results"

# What "fast enough" means for a single PyME host. Referenced by the promotion gate.
P95_BATCH32_BUDGET_MS = 2000.0


def load_split(name: str, data_dir: Path, limit: int | None = None) -> list[dict]:
    path = data_dir / f"{name}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}. Run: python ml/data/prepare.py")
    records = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            records.append(json.loads(line))
            if limit and len(records) >= limit:
                break
    return records


def load_manifest(data_dir: Path) -> dict:
    path = data_dir / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def compute_metrics(labels: list[int], scores: list[float], threshold: float) -> dict:
    from sklearn.metrics import (
        average_precision_score,
        precision_recall_fscore_support,
        roc_auc_score,
    )

    predictions = [1 if score >= threshold else 0 for score in scores]
    precision, recall, f1, _ = precision_recall_fscore_support(
        labels, predictions, average="binary", zero_division=0
    )

    # AUC is undefined on a single-class split; report null instead of crashing.
    both_classes = len(set(labels)) > 1
    return {
        "precision": round(float(precision), 4),
        "recall": round(float(recall), 4),
        "f1": round(float(f1), 4),
        "pr_auc": round(float(average_precision_score(labels, scores)), 4) if both_classes else None,
        "roc_auc": round(float(roc_auc_score(labels, scores)), 4) if both_classes else None,
        "threshold": threshold,
        "predicted_anomaly_rate": round(sum(predictions) / len(predictions), 4) if predictions else 0.0,
        "actual_anomaly_rate": round(sum(labels) / len(labels), 4) if labels else 0.0,
    }


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(len(ordered) * fraction), len(ordered) - 1)
    return round(ordered[index], 3)


def measure_latency(scorer, messages: list[str], rounds: int = 30) -> dict:
    """p95 for a single record and for a batch of 32, measured on this CPU."""
    sample = messages[: max(rounds, 32)] or ["placeholder"]

    scorer.score(sample[:2])  # warm the graph so the first call is not counted

    single = []
    for message in sample[:rounds]:
        started = time.perf_counter()
        scorer.score([message])
        single.append((time.perf_counter() - started) * 1000)

    batched = []
    for _ in range(max(1, rounds // 3)):
        batch = sample[:32] if len(sample) >= 32 else sample
        started = time.perf_counter()
        scorer.score(batch)
        batched.append((time.perf_counter() - started) * 1000)

    return {
        "p95_single_ms": percentile(single, 0.95),
        "p50_single_ms": percentile(single, 0.50),
        "p95_batch32_ms": percentile(batched, 0.95),
        "p50_batch32_ms": percentile(batched, 0.50),
        "rounds": rounds,
    }


def directory_size_mb(path: Path) -> float | None:
    if not path or not path.exists():
        return None
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return round(total / 1e6, 1)


def resolve_model_dir(pin: ModelPin, cache_dir: Path) -> Path | None:
    candidate = cache_dir / f"models--{pin.repo_id.replace('/', '--')}" / "snapshots" / (pin.revision or "")
    return candidate if candidate.exists() else None


def evaluate_model(
    name: str,
    test_records: list[dict],
    train_records: list[dict],
    threshold: float,
    cache_dir: Path,
    checkpoint: Path | None = None,
    model_key: str | None = None,
    batch_size: int = 64,
    device: str | None = None,
) -> dict:
    from scorer import LogScorer

    messages = [record["message"] for record in test_records]
    labels = [record["label"] for record in test_records]

    if checkpoint:
        pin = ModelPin(
            key=name,
            repo_id=str(checkpoint),
            revision="local",
            license="internal",
            role="finetuned",
        )
        scorer = LogScorer(pin, cache_dir=cache_dir, threshold=threshold, offline=True, device=device)
        model_dir: Path | None = checkpoint
    else:
        pin = get_pin(model_key or name)
        scorer = LogScorer(pin, cache_dir=cache_dir, threshold=threshold, offline=True, device=device)
        model_dir = resolve_model_dir(pin, cache_dir)

    scores: list[float] = []
    started = time.perf_counter()
    for start in range(0, len(messages), batch_size):
        chunk = messages[start : start + batch_size]
        scores.extend(result.score for result in scorer.score(chunk))
        if start and start % (batch_size * 50) == 0:
            print(f"    scored {start:,}/{len(messages):,}")
    total_seconds = time.perf_counter() - started

    metadata = scorer.metadata()
    return {
        "name": name,
        "kind": "transformer",
        "model": {
            "repo_id": metadata["repo_id"],
            "revision": metadata["revision"],
            "license": metadata["license"],
            "anomaly_index": metadata["anomaly_index"],
            "anomaly_index_reason": metadata["anomaly_index_reason"],
        },
        "metrics": compute_metrics(labels, scores, threshold),
        "latency": measure_latency(scorer, messages),
        "throughput_records_per_s": round(len(messages) / total_seconds, 1) if total_seconds else None,
        "model_size_mb": directory_size_mb(model_dir) if model_dir else None,
    }


def evaluate_baseline(
    name: str,
    test_records: list[dict],
    train_records: list[dict],
    threshold: float,
) -> dict:
    messages = [record["message"] for record in test_records]
    labels = [record["label"] for record in test_records]

    started = time.perf_counter()
    if name == "level-keyword":
        scores = level_keyword_scores(messages)
        # A binary indicator needs a threshold inside (0, 1] to mean anything.
        effective_threshold = 0.5
    elif name == "template-count":
        scores = template_count_scores([r["message"] for r in train_records], messages)
        effective_threshold = threshold
    else:
        raise ValueError(f"Unknown baseline '{name}'")
    elapsed = time.perf_counter() - started

    return {
        "name": name,
        "kind": "baseline",
        "model": {"repo_id": None, "revision": None, "license": None},
        "metrics": compute_metrics(labels, scores, effective_threshold),
        "latency": {"note": "not comparable to transformer latency; CPU-cheap by construction"},
        "throughput_records_per_s": round(len(messages) / elapsed, 1) if elapsed else None,
        "model_size_mb": 0.0,
    }


def write_result(result: dict, manifest: dict, results_dir: Path, evaluated_records: int,
                 limit: int | None) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        **result,
        "dataset": {
            "repo": manifest.get("dataset_repo"),
            "revision": manifest.get("dataset_revision"),
            "manifest_sha256": manifest.get("manifest_sha256"),
            "test_sha256": manifest.get("splits", {}).get("test", {}).get("sha256"),
            # A run over the first N records is not comparable to a full run, even
            # on the same manifest. Record it so the promotion gate can refuse.
            "evaluated_records": evaluated_records,
            "limit": limit,
            "full_split": limit is None,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor() or "unknown",
        },
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
    path = results_dir / f"{result['name']}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def print_table(results: list[dict]) -> None:
    header = f"{'model':<22} {'kind':<12} {'P':>6} {'R':>6} {'F1':>6} {'PR-AUC':>7} {'p95/32ms':>9} {'MB':>7}"
    print("\n" + header)
    print("-" * len(header))
    for result in sorted(results, key=lambda r: r["metrics"]["f1"], reverse=True):
        metrics = result["metrics"]
        latency = result.get("latency", {})
        p95 = latency.get("p95_batch32_ms")
        size = result.get("model_size_mb")
        print(
            f"{result['name']:<22} {result['kind']:<12} "
            f"{metrics['precision']:>6.3f} {metrics['recall']:>6.3f} {metrics['f1']:>6.3f} "
            f"{(metrics['pr_auc'] if metrics['pr_auc'] is not None else 0):>7.3f} "
            f"{(p95 if p95 is not None else 0):>9.1f} {(size if size is not None else 0):>7.1f}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", action="append", dest="models", help="registry key (repeatable)")
    parser.add_argument("--checkpoint", type=Path, default=None, help="local fine-tuned checkpoint dir")
    parser.add_argument("--name", default=None, help="result name for --checkpoint")
    parser.add_argument("--baseline", action="append", dest="baselines",
                        choices=["template-count", "level-keyword"])
    parser.add_argument("--all", action="store_true", help="both baselines plus every non-rejected model")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--limit", type=int, default=None, help="cap test records (smoke runs)")
    parser.add_argument("--train-limit", type=int, default=50_000, help="records used to fit the classical baseline")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--device", default=None,
                        help="cpu/cuda for scoring. Default: cuda if available, else cpu. The harness is "
                             "eval-only and never runs in the deployed detector, so using an idle GPU here "
                             "is always a win — unlike detector-service, which must match production hardware")
    args = parser.parse_args()

    device = args.device
    if device is None:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"device     : {device}")

    manifest = load_manifest(args.data_dir)
    test_records = load_split("test", args.data_dir, args.limit)
    train_records = load_split("train", args.data_dir, args.train_limit)
    print(f"test split : {len(test_records):,} records "
          f"({sum(r['label'] for r in test_records):,} anomalies)")
    print(f"manifest   : {manifest.get('manifest_sha256', 'MISSING')}")

    baselines = list(args.baselines or [])
    models = list(args.models or [])
    if args.all:
        baselines = sorted(set(baselines) | {"level-keyword", "template-count"})
        # 'rejected' models are excluded on purpose — see models.yaml. Registry
        # roles are "baseline" and "finetune" (models.yaml), not "finetuned" —
        # that mismatch silently limited --all to baseline-bert alone, and
        # combined with the overwrite this replaced, an explicit --model passed
        # alongside --all was discarded rather than added.
        models = sorted(set(models) | {pin.key for pin in iter_pins() if pin.role in {"baseline", "finetune"}})

    results: list[dict] = []

    for name in baselines:
        print(f"\n=== baseline: {name} ===")
        result = evaluate_baseline(name, test_records, train_records, args.threshold)
        write_result(result, manifest, args.results_dir, len(test_records), args.limit)
        results.append(result)

    for key in models:
        print(f"\n=== model: {key} ===")
        try:
            result = evaluate_model(key, test_records, train_records, args.threshold, args.cache_dir,
                                    model_key=key, device=device)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! skipped: {exc}")
            continue
        write_result(result, manifest, args.results_dir, len(test_records), args.limit)
        results.append(result)

    if args.checkpoint:
        name = args.name or args.checkpoint.name
        print(f"\n=== checkpoint: {name} ===")
        result = evaluate_model(name, test_records, train_records, args.threshold, args.cache_dir,
                                checkpoint=args.checkpoint, device=device)
        write_result(result, manifest, args.results_dir, len(test_records), args.limit)
        results.append(result)

    if not results:
        print("Nothing evaluated. Pass --model, --baseline, --checkpoint or --all.", file=sys.stderr)
        return 1

    print_table(results)
    print(f"\nresults written to {args.results_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
