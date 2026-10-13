"""Prepare the web app for the integrated demo: one collector and one node per demo host.

Runs on the host against the app's published API, after the app was seeded
without its fictitious nodes and alerts (demo.sh does both). Stdlib only.

    python3 tools/app_setup.py --api http://localhost:8080 --out deploy/demo/app-credentials.env

Idempotent. A collector's credential is returned once, at creation: it is kept
in --out (mode 600, gitignored), and a collector that already exists but is
missing from that file gets its credential rotated, so the bridge can always
send its heartbeat. Also registers the detector's model, so the model page shows
what actually scores the logs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ADMIN_EMAIL = "admin@anomalywatch.local"
ADMIN_PASSWORD = "password123"  # DefaultUsersSeeder in the app

# host: (display name, ip, operating system, type, environment)
FLEET = {
    "web1": ("Web 1 - tienda", "10.0.1.11", "Ubuntu 24.04 LTS", "SERVER", "PRODUCTION"),
    "web2": ("Web 2 - tienda", "10.0.1.12", "Ubuntu 24.04 LTS", "SERVER", "PRODUCTION"),
    "auth1": ("Auth 1 - SSO", "10.0.1.20", "Debian 12", "SERVER", "PRODUCTION"),
    "db1": ("DB 1 - Postgres", "10.0.2.10", "Debian 12", "DATABASE", "PRODUCTION"),
    "mail1": ("Mail 1 - correo y webmail", "10.0.3.10", "Debian 12", "SERVER", "PRODUCTION"),
}
# Rules the demo adds on top of the app's seeded ones (SSH Brute Force, CRITICAL;
# Privilege Escalation, HIGH), so `demo.sh attack` reaches every severity. Their
# patterns stay clear of the background traffic: loggen's background Postgres
# failures are for user "postgres", and no background line carries "../".
RULES = [
    {"code": "rule-demo-path-traversal", "name": "Path Traversal", "severity": "HIGH",
     "description": "Request that climbs out of the web root, e.g. /../../etc/passwd.",
     "criteria": {"pattern": r"\.\./|/etc/passwd", "minOccurrences": 1, "windowMinutes": 15,
                  "category": "Web Attack",
                  "mitreTechniques": [{"techniqueId": "T1190", "name": "Exploit Public-Facing Application",
                                       "tactic": "Initial Access"}]}},
    {"code": "rule-demo-db-brute-force", "name": "Database Brute Force", "severity": "HIGH",
     "description": "Repeated failed database logins for privileged accounts.",
     "criteria": {"pattern": 'password authentication failed for user "(admin|sa|dbadmin|root)"',
                  "minOccurrences": 5, "windowMinutes": 5, "category": "Authentication",
                  "mitreTechniques": [{"techniqueId": "T1110", "name": "Brute Force",
                                       "tactic": "Credential Access"}]}},
]
MODEL = {"code": "modernbert-ft-v3", "name": "ModernBERT fine-tuned v3", "kind": "OTHER",
         "version": "e1d3703d", "stage": "ACTIVE"}


def call(api: str, method: str, path: str, token: str | None = None, body: dict | None = None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{api}{path}", data, headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        return error.code, None


def login(api: str, wait_seconds: int) -> str:
    """Retries until the API answers: right after `up` it is still starting or seeding."""
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            status, body = call(api, "POST", "/api/v1/auth/login", body={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
            if status == 200:
                return body["accessToken"]
            reason = f"login answered {status}"
        except OSError as error:
            reason = str(error)
        if time.monotonic() > deadline:
            sys.exit(f"The app never accepted the admin login ({reason}). Was it seeded?")
        print(f"Waiting for the app API at {api}: {reason}", flush=True)
        time.sleep(5)


def read_credentials(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://localhost:8080")
    parser.add_argument("--out", type=Path, default=Path("deploy/demo/app-credentials.env"))
    parser.add_argument("--wait", type=int, default=300, help="seconds to wait for the API")
    args = parser.parse_args()
    api = args.api.rstrip("/")

    token = login(api, args.wait)
    credentials = read_credentials(args.out)

    for host, (display_name, ip, operating_system, node_type, environment) in FLEET.items():
        code = f"collector-{host}"
        key = f"COLLECTOR_{host.upper()}"
        status, body = call(api, "POST", "/api/v1/collectors", token,
                            {"code": code, "name": f"FluentBit {host}", "kind": "LINUX", "version": "3.2"})
        if status == 201:
            credentials[key] = f"{code}:{body['agentToken']}"
        elif status == 409 and key not in credentials:
            # Exists, but its credential was only ever shown once and is not here: rotate it.
            _, collectors = call(api, "GET", "/api/v1/collectors", token)
            items = collectors.get("items", collectors) if isinstance(collectors, dict) else collectors
            existing = next(c for c in items if c["code"] == code)
            status, body = call(api, "POST", f"/api/v1/collectors/{existing['id']}/credential", token, {})
            if status != 200:
                sys.exit(f"Could not rotate the credential of {code}: {status}")
            credentials[key] = f"{code}:{body['agentToken']}"
        elif status != 409:
            sys.exit(f"Could not register collector {code}: {status}")

        status, _ = call(api, "POST", "/api/v1/nodes", token, {
            "code": host, "hostname": host, "displayName": display_name, "ip": ip,
            "operatingSystem": operating_system, "type": node_type, "environment": environment, "collector": code})
        if status not in (201, 409):
            sys.exit(f"Could not register node {host}: {status}")
        print(f"{host}: collector {code}, node ready")

    for rule in RULES:
        status, _ = call(api, "POST", "/api/v1/detection-rules", token, rule)
        if status not in (201, 409):
            sys.exit(f"Could not register rule {rule['code']}: {status}")
    print(f"Rules ready: {', '.join(rule['name'] for rule in RULES)}")

    status, _ = call(api, "POST", "/api/v1/ai-models", token, MODEL)
    if status not in (200, 201, 409):
        print(f"Warning: could not register the model ({status}); the demo works without it", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    tmp.write_text("".join(f"{k}={v}\n" for k, v in sorted(credentials.items())))
    os.chmod(tmp, 0o600)
    tmp.replace(args.out)
    print(f"Credentials in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
