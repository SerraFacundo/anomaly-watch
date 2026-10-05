"""Phase-0 smoke test: score a handful of hand-written log lines and print the result.

This is the first real signal the project produces. Eyeball the output before
building anything on top of it — if a kernel panic scores below a routine GET,
the checkpoint is not usable and no amount of pipeline work will fix that.

Usage:
    python ml/smoke_score.py
    python ml/smoke_score.py --model baseline-roberta
    python ml/smoke_score.py --checkpoint /content/drive/MyDrive/modernbert-ft-v3 --name v3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from registry import DEFAULT_CACHE_DIR, ModelPin, get_pin  # noqa: E402
from scorer import LogScorer  # noqa: E402

# Hand-written, deliberately obvious. Expectation is directional, not a test
# assertion — these lines are out of distribution for any public log corpus.
SAMPLES: list[tuple[str, str]] = [
    ("normal", '10.0.0.14 - - [24/Aug/2026:10:12:44 +0000] "GET /index.html HTTP/1.1" 200 5321'),
    ("normal", "Aug 24 10:12:45 web01 systemd[1]: Started Daily apt download activities."),
    ("normal", "Aug 24 10:13:02 web01 sshd[2211]: Accepted publickey for deploy from 10.0.0.9 port 51234 ssh2"),
    ("anomaly", "Aug 24 10:13:11 web01 sshd[2213]: Failed password for invalid user admin from 203.0.113.7 port 44100 ssh2"),
    ("anomaly", "Aug 24 10:13:12 web01 sshd[2214]: Failed password for invalid user root from 203.0.113.7 port 44102 ssh2"),
    ("anomaly", "kernel: Kernel panic - not syncing: Attempted to kill init! exitcode=0x00000009"),
    ("anomaly", "Aug 24 10:14:00 db01 postgres[901]: FATAL: could not write to file pg_wal/000000010000000000000042: No space left on device"),
    ("anomaly", "ERROR dfs.DataNode$DataXceiver: writeBlock blk_-1608999687919862906 received exception java.io.IOException: Broken pipe"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=None, help="registry key (default: registry 'default')")
    parser.add_argument("--checkpoint", type=Path, default=None,
                        help="local fine-tuned checkpoint dir, bypassing the registry — "
                             "for a candidate that hasn't been registered/promoted yet")
    parser.add_argument("--name", default=None, help="label for --checkpoint (default: dir name)")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--online", action="store_true", help="allow a Hub fetch if the cache is cold")
    args = parser.parse_args()

    if args.checkpoint:
        # Same escape hatch ml/eval/harness.py already has, and for the same
        # reason: eyeballing a checkpoint's behavior is exactly the check you
        # want to run *before* deciding it's worth registering or promoting.
        pin = ModelPin(
            key=args.name or args.checkpoint.name,
            repo_id=str(args.checkpoint),
            revision="local",
            license="internal",
            role="finetuned",
        )
    else:
        pin = get_pin(args.model)
    scorer = LogScorer(pin, cache_dir=args.cache_dir, threshold=args.threshold, offline=not args.online)

    meta = scorer.metadata()
    print(f"model      : {meta['repo_id']}")
    print(f"revision   : {meta['revision']}")
    print(f"anomaly ix : {meta['anomaly_index']} ({meta['anomaly_index_reason']})")
    print(f"threshold  : {meta['threshold']}\n")

    results = scorer.score([text for _, text in SAMPLES])

    print(f"{'expected':<9} {'score':>7}  {'flag':<8} message")
    print("-" * 100)
    agree = 0
    for (expected, text), result in zip(SAMPLES, results, strict=True):
        flag = "ANOMALY" if result.is_anomaly else "normal"
        if (expected == "anomaly") == result.is_anomaly:
            agree += 1
        print(f"{expected:<9} {result.score:>7.4f}  {flag:<8} {text[:70]}")

    print(f"\nDirectional agreement: {agree}/{len(SAMPLES)}")
    print(
        "\nThis is a smoke test, not an evaluation. These lines are out of distribution\n"
        "for the public corpora these checkpoints were trained on. The number that\n"
        "matters comes from ml/eval/harness.py on a held-out labeled split."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
