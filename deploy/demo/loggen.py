"""Demo traffic for the multi-node overlay.

Writes plausible nginx, sshd and syslog lines into /logs/<node>/{access,auth}.log
and /logs/<node>/syslog, where each node's FluentBit agent tails them. Stdlib
only, so it runs on a bare python:3.12-slim with nothing installed.

    python loggen.py run                             # normal traffic + background problems, forever
    python loggen.py warmup                          # a burst of background problems, then exit
    python loggen.py attack --node web1              # SSH brute force on web1
    python loggen.py attack --node web2 --kind web   # SQLi / traversal scan
    python loggen.py attack --node db1 --kind panic  # kernel panic: v3 does not flag it

Every problem line below was scored against v3 before it went in here — see
openspec/changes/integrated-demo/fleet-scores.md. The background set holds only
lines v3 flags; the panic set holds only lines it lets through.
"""

from __future__ import annotations

import argparse
import random
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path("/logs")
# ponytail: fixed topology, it has to match the agents in compose.demo.yml anyway.
NODES = {
    "web1": ("nginx", "sshd"),
    "web2": ("nginx", "sshd"),
    "auth1": ("sshd",),
    "db1": ("sshd", "postgres"),
    "mail1": ("nginx", "sshd", "postfix"),
}
# The two troubled nodes: low-intensity problems in the background, healthy otherwise.
TROUBLED = ("db1", "mail1")
FILES = {"nginx": "access.log", "sshd": "auth.log", "postgres": "syslog", "postfix": "syslog", "kernel": "syslog"}

PATHS = ["/", "/products", "/products/42", "/cart", "/login", "/static/app.js", "/static/site.css", "/api/items?page=2"]
WEBMAIL_PATHS = ["/webmail/", "/webmail/?_task=mail", "/webmail/?_task=mail&_mbox=INBOX", "/webmail/skins/elastic/styles.css"]
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
# Webmail probes v3 flags (fleet-scores.md): /.git/config scored 0.001 and is left out.
WEB_PROBES = [("GET", "/wp-login.php", 404), ("GET", "/phpmyadmin/", 404), ("POST", "/xmlrpc.php", 403)]
# In the order a real panic prints: oops, registers, trace, then the panic and its footer.
PANIC = [
    "BUG: unable to handle page fault for address: ffffffffc0a1b2c3",
    "Oops: 0000 [#1] PREEMPT SMP PTI",
    "RIP: 0010:ext4_es_lookup_extent+0x6d/0x230",
    "Kernel panic - not syncing: Fatal exception in interrupt",
    "---[ end Kernel panic - not syncing: Fatal exception in interrupt ]---",
]
# Seconds between background problems on each troubled node. The app's SSH brute
# force rule opens an alert at 5 failures in 5 minutes; one every 90 s or more
# keeps the background SSH below it, so only the live attack turns Critical.
BACKGROUND_EVERY = 90


def ip(private: bool = True) -> str:
    return f"10.0.{random.randint(0, 3)}.{random.randint(2, 254)}" if private else f"203.0.113.{random.randint(2, 254)}"


def nginx_line(path: str, status: int, client: str, agent: str, method: str = "GET") -> str:
    now = datetime.now(UTC).strftime("%d/%b/%Y:%H:%M:%S +0000")
    return f'{client} - - [{now}] "{method} {path} HTTP/1.1" {status} {random.randint(200, 9000)} "-" "{agent}"'


def syslog_line(node: str, program: str, text: str) -> str:
    now = datetime.now(UTC).strftime("%b %e %H:%M:%S")
    tag = program if program == "kernel" else f"{program}[{random.randint(1000, 65000)}]"
    return f"{now} {node} {tag}: {text}"


def sshd_line(node: str, text: str) -> str:
    return syslog_line(node, "sshd", text)


def write(node: str, kind: str, line: str) -> None:
    path = ROOT / node / FILES[kind]
    path.parent.mkdir(parents=True, exist_ok=True)
    # One append per line: two writers (run + attack) share these files.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def normal(node: str, kind: str) -> str:
    if kind == "nginx":
        status = random.choices([200, 304, 404], weights=[90, 8, 2])[0]
        paths = WEBMAIL_PATHS if node == "mail1" else PATHS
        return nginx_line(random.choice(paths), status, ip(), random.choice(AGENTS))
    if kind == "postgres":
        return syslog_line(node, "postgres", random.choice([
            "LOG:  checkpoint starting: time",
            f"LOG:  checkpoint complete: wrote {random.randint(50, 900)} buffers (2.5%); 0 WAL file(s) added, 0 removed, 1 recycled",
            "LOG:  connection authorized: user=app database=shop application_name=api",
        ]))
    if kind == "postfix":
        queue_id = "".join(random.choices("0123456789ABCDEF", k=10))
        return syslog_line(node, random.choice(["postfix/smtp", "postfix/qmgr"]), random.choice([
            f"{queue_id}: to=<ventas@cliente.com.ar>, relay=mx.cliente.com.ar[10.0.0.30]:25, delay=0.4, status=sent (250 2.0.0 Ok: queued)",
            f"{queue_id}: removed",
        ]))
    user = random.choice(USERS)
    return sshd_line(node, random.choice([
        f"Accepted publickey for {user} from {ip()} port {random.randint(30000, 60000)} ssh2",
        f"pam_unix(sshd:session): session opened for user {user}(uid=1000) by (uid=0)",
        f"pam_unix(sshd:session): session closed for user {user}",
    ]))


def background(node: str, ssh: bool = True) -> tuple[str, str]:
    """One low-intensity problem v3 flags, as (kind, line). ssh=False leaves SSH out."""
    stranger = ip(private=False)
    port = random.randint(30000, 60000)
    if node == "db1":
        options = [("postgres", syslog_line(node, "postgres", 'FATAL:  password authentication failed for user "postgres"'))]
        ssh_options = [
            ("sshd", sshd_line(node, f"Failed password for root from {stranger} port {port} ssh2")),
            ("sshd", sshd_line(node, f"Invalid user oracle from {stranger} port {port}")),
        ]
    else:
        method, path, status = random.choice(WEB_PROBES)
        options = [("nginx", nginx_line(path, status, stranger, "Mozilla/5.0", method))]
        ssh_options = [("sshd", sshd_line(node, f"Failed password for invalid user test from {stranger} port {port} ssh2"))]
    return random.choice(options + ssh_options if ssh else options)


def attack(node: str, kind: str, count: int) -> list[str]:
    attacker = ip(private=False)
    if kind == "ssh":
        return [
            sshd_line(node, f"Failed password for invalid user {random.choice(BAD_USERS)} from {attacker} port {random.randint(30000, 60000)} ssh2")
            for _ in range(count)
        ]
    if kind == "panic":
        return [syslog_line(node, "kernel", text) for text in PANIC]
    return [nginx_line(random.choice(WEB_ATTACKS), random.choice([400, 403, 404]), attacker, "sqlmap/1.8") for _ in range(count)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--rate", type=float, default=4.0, help="lines per second across all nodes")
    warm = sub.add_parser("warmup")
    warm.add_argument("--count", type=int, default=4, help="background problems per troubled node")
    hit = sub.add_parser("attack")
    hit.add_argument("--node", required=True, choices=NODES)
    hit.add_argument("--kind", default="ssh", choices=["ssh", "web", "panic"])
    hit.add_argument("--count", type=int, default=40)
    args = parser.parse_args()

    if args.command == "attack":
        if args.kind == "web" and "nginx" not in NODES[args.node]:
            parser.error(f"{args.node} has no nginx")
        file_kind = {"web": "nginx", "ssh": "sshd", "panic": "kernel"}[args.kind]
        lines = attack(args.node, args.kind, args.count)
        for line in lines:
            write(args.node, file_kind, line)
            time.sleep(0.2)
        print(f"{len(lines)} {args.kind} attack lines written to {args.node}")
        return 0

    if args.command == "warmup":
        # No SSH here: added to the background SSH of `run`, a warm-up burst could reach
        # the brute force rule's 5 failures in 5 minutes and open a Critical before the live attack.
        for node in TROUBLED:
            for _ in range(args.count):
                write(node, *background(node, ssh=False))
                time.sleep(0.5)
        print(f"{args.count} background problems written to each of {', '.join(TROUBLED)}")
        return 0

    pairs = [(node, kind) for node, kinds in NODES.items() for kind in kinds]
    next_problem = {node: time.monotonic() + random.uniform(10, BACKGROUND_EVERY) for node in TROUBLED}
    while True:
        node, kind = random.choice(pairs)
        write(node, kind, normal(node, kind))
        for troubled, due in next_problem.items():
            if time.monotonic() >= due:
                write(troubled, *background(troubled))
                next_problem[troubled] = time.monotonic() + random.uniform(BACKGROUND_EVERY, 2 * BACKGROUND_EVERY)
        time.sleep(1 / args.rate)


if __name__ == "__main__":
    raise SystemExit(main())
