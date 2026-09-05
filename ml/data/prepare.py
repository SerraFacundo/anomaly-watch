"""Build a labeled, chronologically-split log dataset from LogHub 2.0.

Source: https://hf.co/datasets/bolu61/loghub_2 — per-system raw text files that
preserve the original LogHub alert-label prefix ('-' = normal, anything else is
an alert category). The dataset's parquet export flattens everything into one
unlabeled `text` column, so we read the raw per-system files instead.

Two things this script is careful about:

1. **Label leakage.** BGL lines carry a `Level` field whose value is almost
   exactly the label (FATAL -> 94% anomaly, INFO -> 100% normal, measured on a
   440k-line slice). Scoring text that contains it would produce a near-perfect
   F1 that reflects reading one word, not understanding a log. Only the Content
   field is scored; Level/Component/Node travel as metadata.

2. **Base rate.** Anomalies in BGL arrive in dense bursts, so a head slice is
   wildly unrepresentative (42% anomalies in the first 60MB vs ~7% overall).
   Sampling is therefore strided across the whole file, which preserves both the
   base rate and chronological order, and is deterministic.

Usage:
    python ml/data/prepare.py
    python ml/data/prepare.py --corpus bgl --stride 8 --max-records 500000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO_ROOT / "ml" / "data" / "build"

DATASET_REPO = "bolu61/loghub_2"

NORMAL_LABEL = "-"


@dataclass(frozen=True)
class CorpusSpec:
    """How to slice one LogHub system file into fields.

    LogHub keeps a fixed leading field layout per system; `content_index` is the
    first field of the actual message. `maxsplit` equals content_index so the
    message keeps its internal spaces.
    """

    name: str
    path_in_repo: str
    content_index: int
    # field index -> metadata name. Never fed to the model.
    metadata_fields: dict[int, str]


CORPORA: dict[str, CorpusSpec] = {
    # Label Timestamp Date Node Time NodeRepeat Type Component Level Content
    "bgl": CorpusSpec(
        name="bgl",
        path_in_repo="data/bgl.txt",
        content_index=9,
        metadata_fields={1: "unix_ts", 2: "date", 3: "node", 6: "type", 7: "component", 8: "level"},
    ),
    # Label Id Date AdminDomain Month Day Time AdminDomain2 Content
    #
    # NOT in DEFAULT_CORPORA. The LogHub 2.0 Thunderbird file carries 44 labeled
    # anomalies across 5.38M lines (0.0008%) — measured, not assumed. Including it
    # adds a few hundred thousand normals and essentially no positives, which drags
    # the test-set anomaly rate from 7.2% down to 3.3% and makes every metric look
    # better for no reason. Opt in with --corpus thunderbird if you want it.
    "thunderbird": CorpusSpec(
        name="thunderbird",
        path_in_repo="data/thunderbird.txt",
        content_index=8,
        metadata_fields={1: "unix_ts", 2: "date", 3: "node", 6: "time", 7: "source"},
    ),
}

# BGL alone: ~4.6M lines at a true ~7% anomaly rate, with real alert categories.
DEFAULT_CORPORA = ["bgl"]

SPLIT_FRACTIONS = (("train", 0.70), ("validation", 0.15), ("test", 0.15))


def resolve_dataset_revision() -> str:
    from huggingface_hub import HfApi

    return HfApi().dataset_info(DATASET_REPO).sha


def fetch_corpus(spec: CorpusSpec, revision: str) -> Path:
    from huggingface_hub import hf_hub_download

    return Path(
        hf_hub_download(
            repo_id=DATASET_REPO,
            filename=spec.path_in_repo,
            repo_type="dataset",
            revision=revision,
        )
    )


def parse_line(line: str, spec: CorpusSpec) -> dict | None:
    parts = line.rstrip("\n").split(" ", spec.content_index)
    if len(parts) <= spec.content_index:
        return None  # truncated or malformed line
    content = parts[spec.content_index].strip()
    if not content:
        return None

    label_field = parts[0]
    return {
        "message": content,
        "label": 0 if label_field == NORMAL_LABEL else 1,
        # Kept for error analysis: which kind of failure, not just that it failed.
        "alert_category": None if label_field == NORMAL_LABEL else label_field,
        "corpus": spec.name,
        "metadata": {name: parts[index] for index, name in spec.metadata_fields.items() if index < len(parts)},
    }


def iter_records(path: Path, spec: CorpusSpec, stride: int, max_records: int) -> Iterator[dict]:
    """Strided read: every `stride`-th line, in file order.

    File order is chronological for both corpora, so this preserves time ordering
    while bounding size and holding the class base rate roughly constant.
    """
    kept = 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle):
            if index % stride:
                continue
            record = parse_line(line, spec)
            if record is None:
                continue
            yield record
            kept += 1
            if kept >= max_records:
                return


def chronological_split(records: list[dict]) -> dict[str, list[dict]]:
    """Split by position, not at random.

    A random split lets the model see a burst's start in train and its tail in
    test, which inflates every metric. The tail is held out instead.
    """
    total = len(records)
    splits: dict[str, list[dict]] = {}
    start = 0
    for name, fraction in SPLIT_FRACTIONS:
        end = total if name == SPLIT_FRACTIONS[-1][0] else start + int(total * fraction)
        splits[name] = records[start:end]
        start = end
    return splits


def write_jsonl(records: list[dict], path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            # sort_keys so the bytes are stable across runs and Python versions.
            line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            handle.write(line)
            digest.update(line.encode("utf-8"))
    return digest.hexdigest()


def class_balance(records: list[dict]) -> dict[str, float | int]:
    anomalies = sum(record["label"] for record in records)
    total = len(records)
    return {
        "total": total,
        "anomalies": anomalies,
        "normals": total - anomalies,
        "anomaly_rate": round(anomalies / total, 6) if total else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", action="append", dest="corpora", choices=sorted(CORPORA))
    parser.add_argument("--stride", type=int, default=16, help="keep every Nth line (default: 16)")
    parser.add_argument("--max-records", type=int, default=300_000, help="cap per corpus")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--revision", default=None, help="dataset revision SHA (default: resolve latest)")
    args = parser.parse_args()

    if args.stride < 1:
        parser.error("--stride must be >= 1")

    selected = [CORPORA[name] for name in (args.corpora or DEFAULT_CORPORA)]
    revision = args.revision or resolve_dataset_revision()
    print(f"dataset {DATASET_REPO} @ {revision}")

    records: list[dict] = []
    per_corpus: dict[str, dict] = {}

    for spec in selected:
        print(f"\n=== {spec.name} ===")
        path = fetch_corpus(spec, revision)
        print(f"  source: {path} ({path.stat().st_size / 1e6:.0f} MB)")
        corpus_records = list(iter_records(path, spec, args.stride, args.max_records))
        balance = class_balance(corpus_records)
        print(f"  kept {balance['total']:,} records, anomaly rate {balance['anomaly_rate']:.2%}")
        if balance["anomalies"] == 0:
            print("  ! no anomalies sampled — the split would be unusable. Lower --stride.")
        per_corpus[spec.name] = {
            **balance,
            "stride": args.stride,
            "content_index": spec.content_index,
            "metadata_fields": {str(k): v for k, v in spec.metadata_fields.items()},
        }
        records.extend(corpus_records)

    if not records:
        print("No records produced.", file=sys.stderr)
        return 1

    # Interleaving corpora would break chronology, so keep each corpus contiguous
    # and split each one independently, then concatenate per split.
    splits: dict[str, list[dict]] = {name: [] for name, _ in SPLIT_FRACTIONS}
    for spec in selected:
        corpus_records = [record for record in records if record["corpus"] == spec.name]
        for name, chunk in chronological_split(corpus_records).items():
            splits[name].extend(chunk)

    args.out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "dataset_repo": DATASET_REPO,
        "dataset_revision": revision,
        "corpora": per_corpus,
        "stride": args.stride,
        "max_records_per_corpus": args.max_records,
        "split_fractions": {name: fraction for name, fraction in SPLIT_FRACTIONS},
        "splits": {},
        "notes": {
            "label_rule": "'-' in the leading field means normal; any other value is an alert category.",
            "leakage": (
                "Only the Content field is exposed as `message`. BGL's Level field is a near-perfect "
                "label proxy (FATAL -> ~94% anomaly, INFO -> 100% normal) and is kept in metadata only. "
                "ml/eval/harness.py evaluates a level-only baseline so this shortcut stays visible."
            ),
            "sampling": "Strided across the whole file, preserving chronological order and base rate.",
            "split": "Chronological by position, per corpus. The tail is the test split.",
            "hdfs": (
                "HDFS is deliberately excluded: its labels are per block_id in anomaly_label.csv, i.e. "
                "session-level, not per-line. It belongs to the deferred windowed-detection phase."
            ),
            "thunderbird": (
                "Thunderbird is excluded by default: 44 labeled anomalies across 5.38M lines "
                "(0.0008%). It adds normals and no signal, and dilutes the test-set base rate."
            ),
        },
    }

    for name, split_records in splits.items():
        path = args.out / f"{name}.jsonl"
        digest = write_jsonl(split_records, path)
        manifest["splits"][name] = {**class_balance(split_records), "sha256": digest, "path": path.name}
        print(f"  {name:<11} {len(split_records):>8,} records  sha256={digest[:16]}…")

    # The manifest hash identifies the exact data an eval number was produced on.
    manifest_bytes = json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8")
    manifest["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    (args.out / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )

    print(f"\nmanifest: {args.out / 'manifest.json'}")
    print(f"manifest_sha256: {manifest['manifest_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
