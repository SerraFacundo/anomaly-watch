"""Score the lines the demo fleet will emit, against the detector that will score them.

The integrated demo needs two facts before its script is written: the kernel
panic shown live must really go unflagged, and the background problems of the
two troubled nodes must really be flagged. Both are claims about v3, so this
asks the running detector instead of trusting the README. Stdlib only, like
tools/detector_latency.py, so it runs inside the detector container:

    podman compose exec -T detector python - < tools/score_fleet_lines.py

Lines are written exactly as they reach the model: FluentBit ships the raw line
untouched, timestamp and hostname included. Prints a Markdown table to paste
into the ticket, then a one-line verdict per expectation.
"""

from __future__ import annotations

import json
import os
import urllib.request

ENDPOINT = os.environ.get("DETECTOR_URL", "http://localhost:8003")

TS = "Oct 13 10:14:05"

# (scenario, expectation, line). "flag" means the demo relies on v3 marking it;
# "pass" means the demo relies on v3 letting it through.
LINES = [
    # Kernel panic shown live: the demo's honest limit.
    ("panic", "pass", f"{TS} db1 kernel: Kernel panic - not syncing: Fatal exception in interrupt"),
    ("panic", "pass", f"{TS} db1 kernel: Kernel panic - not syncing: VFS: Unable to mount root fs on unknown-block(0,0)"),
    ("panic", "pass", f"{TS} db1 kernel: BUG: unable to handle page fault for address: ffffffffc0a1b2c3"),
    ("panic", "pass", f"{TS} db1 kernel: Oops: 0000 [#1] PREEMPT SMP PTI"),
    ("panic", "pass", f"{TS} db1 kernel: RIP: 0010:ext4_es_lookup_extent+0x6d/0x230"),
    ("panic", "pass", f"{TS} db1 kernel: ---[ end Kernel panic - not syncing: Fatal exception in interrupt ]---"),
    # Other operational failures, for the closing remark on what v3 does not see.
    ("operational", "pass", f"{TS} db1 kernel: Out of memory: Killed process 4211 (postgres) total-vm:8123456kB, anon-rss:6012344kB"),
    ("operational", "pass", f"{TS} db1 kernel: EXT4-fs error (device sda1): ext4_find_entry:1455: inode #2: comm postgres: reading directory lblock 0"),
    ("operational", "pass", f"{TS} db1 kernel: blk_update_request: I/O error, dev sda, sector 123456 op 0x1:(WRITE) flags 0x800 phys_seg 1 prio class 0"),
    ("operational", "pass", f'{TS} db1 postgres[901]: PANIC:  could not write to file "pg_wal/xlogtemp.901": No space left on device'),
    ("operational", "pass", f"{TS} db1 systemd[1]: postgresql.service: Main process exited, code=killed, status=9/KILL"),
    ("operational", "pass", f"{TS} db1 systemd[1]: postgresql.service: Failed with result 'oom-kill'."),
    ("operational", "pass", f"{TS} mail1 postfix/smtpd[2210]: warning: not enough free space in mail queue: 10485760 bytes < 1.5*message size limit"),
    # Low-intensity background problems for db1 and mail1: the demo needs these flagged.
    ("background", "flag", f"{TS} db1 sshd[3411]: Failed password for root from 203.0.113.41 port 52144 ssh2"),
    ("background", "flag", f"{TS} db1 sshd[3412]: Invalid user oracle from 198.51.100.23 port 40122"),
    ("background", "flag", f"{TS} db1 sshd[3413]: Connection closed by authenticating user root 203.0.113.41 port 52150 [preauth]"),
    ("background", "flag", f'{TS} db1 postgres[1201]: FATAL:  password authentication failed for user "postgres"'),
    ("background", "flag", f"{TS} mail1 postfix/smtpd[2211]: warning: unknown[203.0.113.77]: SASL LOGIN authentication failed: UGFzc3dvcmQ6"),
    ("background", "flag", f"{TS} mail1 postfix/smtpd[2212]: NOQUEUE: reject: RCPT from unknown[198.51.100.9]: 554 5.7.1 <spam@example.net>: Relay access denied; from=<a@b.c> to=<spam@example.net> proto=ESMTP helo=<x>"),
    ("background", "flag", f"{TS} mail1 sshd[3420]: Failed password for invalid user test from 192.0.2.66 port 33012 ssh2"),
    ("background", "flag", '198.51.100.14 - - [13/Oct/2026:10:14:05 +0000] "GET /wp-login.php HTTP/1.1" 404 162 "-" "Mozilla/5.0"'),
    ("background", "flag", '198.51.100.14 - - [13/Oct/2026:10:14:06 +0000] "GET /.git/config HTTP/1.1" 404 162 "-" "Mozilla/5.0"'),
    ("background", "flag", '198.51.100.14 - - [13/Oct/2026:10:14:07 +0000] "GET /phpmyadmin/ HTTP/1.1" 404 162 "-" "Mozilla/5.0"'),
    ("background", "flag", '198.51.100.14 - - [13/Oct/2026:10:14:08 +0000] "POST /xmlrpc.php HTTP/1.1" 403 162 "-" "Mozilla/5.0"'),
    # Normal traffic of the new nodes: must stay quiet, or db1 and mail1 drown in noise.
    ("normal", "pass", f"{TS} db1 postgres[901]: LOG:  checkpoint starting: time"),
    ("normal", "pass", f"{TS} db1 postgres[901]: LOG:  checkpoint complete: wrote 412 buffers (2.5%); 0 WAL file(s) added, 0 removed, 1 recycled"),
    ("normal", "pass", f"{TS} db1 postgres[1300]: LOG:  connection authorized: user=app database=shop application_name=api"),
    ("normal", "pass", f"{TS} db1 sshd[3500]: Accepted publickey for deploy from 10.0.0.9 port 51234 ssh2: ED25519 SHA256:abc"),
    ("normal", "pass", f"{TS} mail1 postfix/smtp[2300]: 4F2A11C0D3: to=<ventas@cliente.com.ar>, relay=mx.cliente.com.ar[10.0.0.30]:25, delay=0.4, status=sent (250 2.0.0 Ok: queued)"),
    ("normal", "pass", f"{TS} mail1 postfix/qmgr[2301]: 4F2A11C0D3: removed"),
    ("normal", "pass", f"{TS} mail1 systemd[1]: Started Daily apt download activities."),
    ("normal", "pass", f"{TS} mail1 CRON[3120]: pam_unix(cron:session): session opened for user root(uid=0) by (uid=0)"),
]


def main() -> None:
    model = json.load(urllib.request.urlopen(f"{ENDPOINT}/model", timeout=10))
    body = json.dumps({"records": [{"message": line} for _, _, line in LINES]}).encode()
    request = urllib.request.Request(f"{ENDPOINT}/score", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=300) as response:
        results = json.load(response)["results"]

    print(f"Modelo `{model['key']}` revisión `{model['revision'][:8]}`, umbral {model['threshold']}\n")
    print("| escenario | se espera | score | marcada | ok | línea |")
    print("|---|---|---|---|---|---|")
    misses = []
    for (scenario, expect, line), result in zip(LINES, results):
        flagged = result["is_anomaly"]
        ok = flagged == (expect == "flag")
        if not ok:
            misses.append((scenario, expect, line))
        shown = line.replace("|", "\\|")
        print(f"| {scenario} | {expect} | {result['score']:.4f} | {'sí' if flagged else 'no'} | {'ok' if ok else 'FALLA'} | `{shown}` |")

    print()
    for scenario in dict.fromkeys(s for s, _, _ in LINES):
        rows = [(e, l) for s, e, l in LINES if s == scenario]
        bad = sum(1 for s, e, l in misses if s == scenario)
        print(f"{scenario}: {len(rows) - bad}/{len(rows)} como se esperaba")


if __name__ == "__main__":
    main()
