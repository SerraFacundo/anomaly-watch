"""Demo traffic for the multi-node overlay.

Writes plausible nginx and sshd lines into /logs/<node>/{access,auth}.log, where
each node's FluentBit agent tails them. Stdlib only, so it runs on a bare
python:3.12-slim with nothing installed.

    python loggen.py run                       # normal traffic, forever
    python loggen.py attack --node web1        # SSH brute force on web1
    python loggen.py attack --node web2 --kind web   # SQLi / traversal scan
"""

from __future__ import annotations

import argparse
import random
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path("/logs")
# ponytail: fixed topology, it has to match the agents in compose.demo.yml anyway.
NODES = {"web1": ("nginx", "sshd"), "web2": ("nginx", "sshd"), "auth1": ("sshd",)}

PATHS = ["/", "/products", "/products/42", "/cart", "/login", "/static/app.js", "/static/site.css", "/api/items?page=2"]
AGENTS = ["Mozilla/5.0 (X11; Linux x86_64) Firefox/131.0", "Mozilla/5.0 (Windows NT 10.0) Chrome/129.0", "curl/8.5.0"]
USERS = ["deploy", "facundo", "backup"]
BAD_USERS = ["admin", "root", "test", "oracle", "ubuntu", "postgres", "guest", "ftpuser"]
WEB_ATTACKS = [
    "/products?id=1'%20OR%20'1'='1",
    "/login?user=admin'--",
    "/../../../../etc/passwd",
    "/cgi-bin/.%2e/.%2e/bin/sh",
    "/wp-login.php",
    "/.env",
    "/index.php?page=php://filter/convert.base64-encode/resource=config",
    "/search?q=<script>alert(document.cookie)</script>",
]


def ip(private: bool = True) -> str:
    return f"10.0.{random.randint(0, 3)}.{random.randint(2, 254)}" if private else f"203.0.113.{random.randint(2, 254)}"


def nginx_line(path: str, status: int, client: str, agent: str) -> str:
    now = datetime.now(UTC).strftime("%d/%b/%Y:%H:%M:%S +0000")
    return f'{client} - - [{now}] "GET {path} HTTP/1.1" {status} {random.randint(200, 9000)} "-" "{agent}"'


def sshd_line(node: str, text: str) -> str:
    now = datetime.now(UTC).strftime("%b %e %H:%M:%S")
    return f"{now} {node} sshd[{random.randint(1000, 65000)}]: {text}"


def write(node: str, kind: str, line: str) -> None:
    path = ROOT / node / ("access.log" if kind == "nginx" else "auth.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    # One append per line: two writers (run + attack) share these files.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def normal(node: str, kind: str) -> str:
    if kind == "nginx":
        status = random.choices([200, 304, 404], weights=[90, 8, 2])[0]
        return nginx_line(random.choice(PATHS), status, ip(), random.choice(AGENTS))
    user = random.choice(USERS)
    return sshd_line(node, random.choice([
        f"Accepted publickey for {user} from {ip()} port {random.randint(30000, 60000)} ssh2",
        f"pam_unix(sshd:session): session opened for user {user}(uid=1000) by (uid=0)",
        f"pam_unix(sshd:session): session closed for user {user}",
    ]))


def attack(node: str, kind: str, count: int) -> list[str]:
    attacker = ip(private=False)
    if kind == "ssh":
        return [
            sshd_line(node, f"Failed password for invalid user {random.choice(BAD_USERS)} from {attacker} port {random.randint(30000, 60000)} ssh2")
            for _ in range(count)
        ]
    return [nginx_line(random.choice(WEB_ATTACKS), random.choice([400, 403, 404]), attacker, "sqlmap/1.8") for _ in range(count)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--rate", type=float, default=4.0, help="lines per second across all nodes")
    hit = sub.add_parser("attack")
    hit.add_argument("--node", required=True, choices=NODES)
    hit.add_argument("--kind", default="ssh", choices=["ssh", "web"])
    hit.add_argument("--count", type=int, default=40)
    args = parser.parse_args()

    if args.command == "attack":
        if args.kind == "web" and "nginx" not in NODES[args.node]:
            parser.error(f"{args.node} has no nginx")
        for line in attack(args.node, args.kind, args.count):
            write(args.node, "nginx" if args.kind == "web" else "sshd", line)
            time.sleep(0.2)
        print(f"{args.count} {args.kind} attack lines written to {args.node}")
        return 0

    pairs = [(node, kind) for node, kinds in NODES.items() for kind in kinds]
    while True:
        node, kind = random.choice(pairs)
        write(node, kind, normal(node, kind))
        time.sleep(1 / args.rate)


if __name__ == "__main__":
    raise SystemExit(main())
