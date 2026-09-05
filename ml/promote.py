"""Promotion gate: decide whether a candidate model may become the default.

A model is promoted only if it beats the incumbent on F1 without blowing the
latency budget. Making this a script rather than a paragraph means the rule is
applied the same way every time and the reasoning is printed, not remembered.

Promotion itself is deliberately just a config edit — `default:` in models.yaml.

Usage:
    python ml/promote.py --candidate modernbert-ft
    python ml/promote.py --candidate modernbert-ft --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "ml"))

from registry import REGISTRY_PATH, load_registry  # noqa: E402

RESULTS_DIR = REPO_ROOT / "ml" / "eval" / "results"
P95_BATCH32_BUDGET_MS = 2000.0


def load_result(name: str, results_dir: Path) -> dict:
    path = results_dir / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No eval result for '{name}' at {path}. "
            f"Run: python ml/eval/harness.py --model {name}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def set_default(key: str, path: Path = REGISTRY_PATH) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("default:"):
            lines[index] = f"default: {key}\n"
            path.write_text("".join(lines), encoding="utf-8")
            return
    raise RuntimeError(f"No 'default:' key found in {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--incumbent", default=None, help="defaults to models.yaml 'default:'")
    parser.add_argument("--budget-ms", type=float, default=P95_BATCH32_BUDGET_MS)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--apply", action="store_true", help="write the promotion into models.yaml")
    args = parser.parse_args()

    incumbent_key = args.incumbent or load_registry().get("default")
    candidate = load_result(args.candidate, args.results_dir)

    try:
        incumbent = load_result(incumbent_key, args.results_dir)
    except FileNotFoundError:
        incumbent = None

    candidate_f1 = candidate["metrics"]["f1"]
    candidate_p95 = (candidate.get("latency") or {}).get("p95_batch32_ms")

    print(f"candidate : {args.candidate}  F1={candidate_f1}  p95/32={candidate_p95}")
    if incumbent:
        print(f"incumbent : {incumbent_key}  F1={incumbent['metrics']['f1']}  "
              f"p95/32={(incumbent.get('latency') or {}).get('p95_batch32_ms')}")
    else:
        print(f"incumbent : {incumbent_key} (no eval result on file)")

    # Comparing numbers produced on different data would be meaningless.
    candidate_manifest = (candidate.get("dataset") or {}).get("manifest_sha256")
    if incumbent:
        incumbent_manifest = (incumbent.get("dataset") or {}).get("manifest_sha256")
        if candidate_manifest != incumbent_manifest:
            print("\nFAIL: candidate and incumbent were evaluated on different datasets.")
            print(f"  candidate manifest: {candidate_manifest}")
            print(f"  incumbent manifest: {incumbent_manifest}")
            print("  Re-run the harness for both against the same build.")
            return 1

    reasons: list[str] = []
    if incumbent and candidate_f1 <= incumbent["metrics"]["f1"]:
        reasons.append(
            f"F1 {candidate_f1} does not beat incumbent {incumbent['metrics']['f1']}"
        )
    if candidate_p95 is not None and candidate_p95 > args.budget_ms:
        reasons.append(f"p95 batch-32 latency {candidate_p95}ms exceeds budget {args.budget_ms}ms")

    # A model that only beats a collapsed or trivial incumbent has proven nothing.
    for floor_name in ("level-keyword", "template-count"):
        if floor_name == args.candidate:
            continue  # a baseline is not required to beat itself
        try:
            floor = load_result(floor_name, args.results_dir)
        except FileNotFoundError:
            print(f"\nnote: no '{floor_name}' baseline on file — run it, the gate is weaker without it")
            continue
        if candidate_f1 <= floor["metrics"]["f1"]:
            reasons.append(
                f"F1 {candidate_f1} does not beat the '{floor_name}' floor {floor['metrics']['f1']}"
            )

    if reasons:
        print("\nGATE: REJECTED")
        for reason in reasons:
            print(f"  - {reason}")
        return 1

    print("\nGATE: PASSED")
    if args.apply:
        set_default(args.candidate)
        print(f"models.yaml default -> {args.candidate}")
        print("Restart detector-service to pick it up.")
    else:
        print(f"Re-run with --apply to set models.yaml default to '{args.candidate}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
