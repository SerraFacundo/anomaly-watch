"""Pull models from the Hub into the local cache and pin their exact revisions.

This is the only script allowed to reach the Hub. The deployed stack runs with
HF_HUB_OFFLINE=1 and reads whatever this script left in the cache directory.

Usage:
    python ml/download_models.py                      # every non-deferred model
    python ml/download_models.py --model baseline-bert
    python ml/download_models.py --role baseline
    python ml/download_models.py --resolve-only       # pin SHAs, skip the weights
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from registry import DEFAULT_CACHE_DIR, ModelPin, get_pin, iter_pins, set_revision  # noqa: E402

# GGUF and other non-transformers artifacts are large and not loaded by the
# detector, so we only ever resolve their SHA.
RESOLVE_ONLY_ROLES = {"deferred"}

ALLOWED_LICENSES = {"apache-2.0", "mit", "bsd-3-clause"}


def resolve_revision(repo_id: str) -> str:
    from huggingface_hub import HfApi

    return HfApi().model_info(repo_id).sha


def download(pin: ModelPin, cache_dir: Path) -> Path:
    from huggingface_hub import snapshot_download

    return Path(
        snapshot_download(
            repo_id=pin.repo_id,
            revision=pin.require_revision(),
            cache_dir=str(cache_dir),
            # .bin duplicates .safetensors; .msgpack/.h5 are Flax/TF copies.
            ignore_patterns=["*.msgpack", "*.h5", "*.ot", "*.bin"],
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", dest="models", help="registry key (repeatable)")
    parser.add_argument("--role", action="append", dest="roles", help="filter by role (repeatable)")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--resolve-only", action="store_true", help="pin revisions without fetching weights")
    parser.add_argument("--include-deferred", action="store_true")
    args = parser.parse_args()

    if args.models:
        pins = [get_pin(key) for key in args.models]
    else:
        pins = iter_pins(roles=set(args.roles) if args.roles else None)
        if not args.include_deferred:
            pins = [pin for pin in pins if pin.role != "deferred"]

    if not pins:
        print("No models matched.", file=sys.stderr)
        return 1

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    failures = 0

    for pin in pins:
        print(f"\n=== {pin.key} — {pin.repo_id} ===")

        if pin.license not in ALLOWED_LICENSES:
            print(f"  ! license '{pin.license}' is not in the permissive allowlist {sorted(ALLOWED_LICENSES)}")
            print("    Self-hosted redistribution needs a permissive license. Skipping.")
            failures += 1
            continue

        try:
            revision = pin.revision or resolve_revision(pin.repo_id)
        except Exception as exc:  # noqa: BLE001 - report and continue to the next model
            print(f"  ! could not resolve revision: {exc}")
            failures += 1
            continue

        if revision != pin.revision:
            set_revision(pin.key, revision)
            print(f"  pinned revision -> {revision}")
        else:
            print(f"  revision already pinned -> {revision}")

        if args.resolve_only or pin.role in RESOLVE_ONLY_ROLES:
            print("  (resolve-only, weights not fetched)")
            continue

        try:
            path = download(ModelPin(**{**pin.__dict__, "revision": revision}), args.cache_dir)
            print(f"  cached at {path}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! download failed: {exc}")
            failures += 1

    print(f"\nCache dir: {args.cache_dir}")
    if failures:
        print(f"{failures} model(s) failed.", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
