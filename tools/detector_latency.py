"""Measure detector-service latency where it actually runs, for the promotion gate.

The harness measures latency in whatever process it runs in — on Colab that
was a 2-vCPU VM 70x slower than the dev machine, for every model alike. This
asks the running detector instead: same container, same DETECTOR_NUM_THREADS,
same hardware the demo uses. Stdlib only, so it runs inside the detector
container without installing anything:

    docker compose exec -T detector python - < tools/detector_latency.py

Prints a `latency` block in the harness's format. `server_*` is the model time
the service reports; the other numbers include HTTP and JSON on top, so they
err pessimistic, which is the right side to err on for a budget.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

ENDPOINT = os.environ.get("DETECTOR_URL", "http://localhost:8003")
ROUNDS = 30

# Representative line lengths for the traffic the demo ships: web, sshd, journal.
SAMPLES = [
    '10.0.0.14 - - [24/Aug/2026:10:12:44 +0000] "GET /index.html HTTP/1.1" 200 5321',
    "Aug 24 10:12:45 web01 systemd[1]: Started Daily apt download activities.",
    "Aug 24 10:13:02 web01 sshd[2211]: Accepted publickey for deploy from 10.0.0.9 port 51234 ssh2",
    "Aug 24 10:13:11 web01 sshd[2213]: Failed password for invalid user admin from 203.0.113.7 port 44100 ssh2",
    '203.0.113.9 - - [24/Aug/2026:10:13:20 +0000] "GET /wp-login.php HTTP/1.1" 404 162 "-" "Mozilla/5.0"',
    "kernel: Kernel panic - not syncing: Attempted to kill init! exitcode=0x00000009",
    "Aug 24 10:14:00 db01 postgres[901]: FATAL: could not write to file pg_wal/000000010000000000000042",
    "Aug 24 10:14:05 web01 CRON[3120]: pam_unix(cron:session): session opened for user root by (uid=0)",
]


def score(messages: list[str]) -> tuple[float, float]:
    body = json.dumps({"records": [{"message": m} for m in messages]}).encode()
    request = urllib.request.Request(f"{ENDPOINT}/score", body, {"Content-Type": "application/json"})
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.load(response)
    return (time.perf_counter() - started) * 1000, payload["elapsed_ms"]


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(int(len(ordered) * fraction), len(ordered) - 1)], 3)


def main() -> None:
    model = json.load(urllib.request.urlopen(f"{ENDPOINT}/model", timeout=10))
    batch = (SAMPLES * 4)[:32]
    score(batch[:2])  # warm-up, not counted

    single = [score([SAMPLES[i % len(SAMPLES)]]) for i in range(ROUNDS)]
    batched = [score(batch) for _ in range(ROUNDS)]

    print(json.dumps({
        "model": model,
        "latency": {
            "p95_single_ms": percentile([s[0] for s in single], 0.95),
            "p50_single_ms": percentile([s[0] for s in single], 0.50),
            "p95_batch32_ms": percentile([b[0] for b in batched], 0.95),
            "p50_batch32_ms": percentile([b[0] for b in batched], 0.50),
            "server_p95_batch32_ms": percentile([b[1] for b in batched], 0.95),
            "rounds": ROUNDS,
            "device": f"cpu (detector-service, DETECTOR_NUM_THREADS={os.environ.get('DETECTOR_NUM_THREADS', 'unset')})",
        },
    }, indent=2))


if __name__ == "__main__":
    main()
