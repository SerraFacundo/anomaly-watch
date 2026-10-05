"""Build a training corpus from the local corpora in `ml/data/raw/`.

Same output shape, splits and manifest as `prepare.py` — those helpers are
imported, not re-implemented. Only the readers are new, because these sources are
a raw nginx access log and a pile of binary Windows event logs rather than
LogHub's pre-labelled text.

Sources (all permissively licensed, see ml/research/log-datasets.md):

  nginx   zanbil.ir access log, CC0. 10.4M lines, no labels — attacks are found
          by rule. The site is a real shop on the public internet, so it really
          is attacked: SQL injection, XSS, wp-login and phpMyAdmin probing.
  winlog    NextronSystems/evtx-baseline (Apache-2.0) as negatives,
            mdecrevoisier/EVTX-to-MITRE-Attack (CC0) as positives.
  weblogs   Zenodo Web Server Log Dataset (MIT, doi:10.5281/zenodo.20001206) —
            real Apache + CyberPanel + auth.log + syslog from two organisations,
            with expert-written ground-truth labelling rules. The only source in
            this corpus with real auth.log attack lines.

Usage:
    python ml/data/prepare_local.py
    python ml/data/prepare_local.py --corpus nginx --stride 8
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Iterator
from urllib.parse import unquote_plus

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from prepare import chronological_split, class_balance, write_jsonl  # noqa: E402

RAW = REPO_ROOT / "ml" / "data" / "raw"
DEFAULT_OUT = REPO_ROOT / "ml" / "data" / "build-local"

# --- nginx ------------------------------------------------------------------

# Attack signatures. Deliberately narrow: a rule that also matches normal traffic
# poisons the negative class, which is worse than missing a few positives.
#
# The 5xx-looking-number trap from services/collector/cache.py applies here too —
# these all anchor on request content, never on a bare number.
ATTACK_RULES: dict[str, re.Pattern] = {
    "sqli": re.compile(r"union\s+(all\s+)?select|select\s.{0,40}\sfrom\s|sleep\(\d|benchmark\(",
                       re.IGNORECASE),
    "xss": re.compile(r"<script|javascript:|onerror\s*=|onload\s*=", re.IGNORECASE),
    "traversal": re.compile(r"\.\./|etc/passwd|boot\.ini", re.IGNORECASE),
    "cms_probe": re.compile(r"/(wp-login\.php|wp-admin|xmlrpc\.php|phpmyadmin|administrator/index\.php)",
                            re.IGNORECASE),
    "config_probe": re.compile(r"/(\.env|\.git/config|\.aws/credentials|config\.php\.bak|web\.config)",
                               re.IGNORECASE),
    "shell": re.compile(r"(shell|c99|r57|webshell)\.php|cmd=|/bin/(ba)?sh|base64_decode\(", re.IGNORECASE),
}

# nginx combined: ip - - [ts] "METHOD path proto" status bytes "referer" "ua" "x"
_NGINX = re.compile(r'^(?P<ip>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] "(?P<req>[^"]*)" (?P<status>\d{3}) ')

# Client addresses are personal data and the model has no use for them: Drain3
# templatises them to <*> anyway. Masked here so they never reach a checkpoint.
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def classify_nginx(line: str) -> str | None:
    """Attack category for a request line, or None if it looks ordinary."""
    match = _NGINX.match(line)
    # Match on the request only. The user-agent and referer are attacker-controlled
    # free text, and matching them flags any visitor who mentions "wp-login".
    request = match.group("req") if match else line
    # Payloads arrive percent-encoded (`UNION%20ALL%20SELECT`). Decoding once is
    # the whole fix; the alternative is every rule growing `%`-tolerant variants.
    haystack = unquote_plus(request)
    for category, pattern in ATTACK_RULES.items():
        if pattern.search(haystack):
            return category
    return None


def read_nginx(path: Path, stride: int, max_records: int) -> Iterator[dict]:
    """Every attack line, plus every `stride`-th normal line.

    Uniform striding would keep ~1 positive in 16 out of a 0.02% base rate, which
    is not a training set. The cost is an inflated anomaly rate in *every* split
    including test, so precision measured on this is optimistic — recorded in the
    manifest rather than left for someone to discover in production.
    """
    kept = 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle):
            category = classify_nginx(line)
            if category is None and index % stride:
                continue
            message = _IPV4.sub("0.0.0.0", line.rstrip("\n")).strip()
            if not message:
                continue
            yield {
                "message": message,
                "label": 0 if category is None else 1,
                "alert_category": category,
                "corpus": "nginx",
                "metadata": {},
            }
            kept += 1
            if kept >= max_records:
                return


# --- windows event log ------------------------------------------------------

# Never rendered into the scored text. Channel and Provider are the leak: the
# attack samples come from channels the benign baseline barely populates, so a
# model given them learns "which log file was this" instead of "is this an
# attack". Same shortcut as BGL's Level field, documented in prepare.py.
EXCLUDED_FIELDS = {"Channel", "Provider", "Level", "Keywords", "Task", "Opcode",
                   "SystemTime", "EventRecordID", "Correlation", "Execution", "Security"}

# Machine and account identity. The attack EVTX were captured on different hosts
# than the benign baseline, so a hostname or a domain SID *is* the label: a
# classifier using nothing but `SubjectDomainName` scores precision 1.000 on this
# corpus. Masked for the same reason client IPs are, and with the same argument —
# the model has no legitimate use for them.
IDENTITY_FIELDS = {"SubjectDomainName", "TargetDomainName", "WorkstationName", "ComputerName",
                   "SubjectUserName", "TargetUserName", "AccountName", "Workstation",
                   "TargetServerName", "ServerName", "Host", "HostName"}

# Domain-relative SIDs name a specific installation. Well-known SIDs (S-1-5-18
# LocalSystem, S-1-5-19/20) are universal and carry real meaning, so they stay.
_DOMAIN_SID = re.compile(r"S-1-5-21(?:-\d+)+")


def render_event(event: dict) -> str | None:
    """One EVTX record as one line: EventID plus its EventData fields."""
    system = event.get("System") or {}
    event_id = system.get("EventID")
    if isinstance(event_id, dict):  # some providers nest it as {"#text": N}
        event_id = event_id.get("#text")
    if event_id is None:
        return None

    data = event.get("EventData") or {}
    if not isinstance(data, dict):
        return None
    fields = []
    for key, value in data.items():
        if key in EXCLUDED_FIELDS or key.startswith("#") or value in (None, "", "-"):
            continue
        # Keep the field — that an event names a workstation is signal — but not
        # which one, because which one is the corpus it came from.
        rendered = "MASKED" if key in IDENTITY_FIELDS else _DOMAIN_SID.sub("S-1-5-21-MASKED", str(value))
        fields.append(f"{key}={rendered}")
    return f"EventID={event_id} " + " ".join(fields) if fields else f"EventID={event_id}"


def shared_event_ids(records: list[dict]) -> set[str]:
    """EventIDs that occur in both classes.

    An EventID seen only in the attack collection is a free answer: 77 of them
    exist here, and memorising them alone scores precision 0.998. Restricting to
    the overlap forces the model to read the fields of an event rather than
    recognise its number. The overlap is where detection actually lives — 4688
    process creation, 4103/4104 PowerShell, 4624 logon, 7045 service install.
    """
    seen: dict[int, set[str]] = {0: set(), 1: set()}
    for record in records:
        match = re.match(r"EventID=(\d+)", record["message"])
        if match:
            seen[record["label"]].add(match.group(1))
    return seen[0] & seen[1]


def read_evtx(root: Path, label: int, corpus: str, max_per_file: int) -> Iterator[dict]:
    import evtx

    for path in sorted(root.rglob("*.evtx")):
        try:
            parser = evtx.PyEvtxParser(str(path))
            for count, record in enumerate(parser.records_json()):
                if count >= max_per_file:
                    break
                event = json.loads(record["data"]).get("Event") or {}
                message = render_event(event)
                if not message:
                    continue
                yield {
                    "message": message,
                    "label": label,
                    "alert_category": path.parent.name if label else None,
                    "corpus": corpus,
                    "metadata": {"timestamp": record.get("timestamp", "")},
                }
        except Exception as exc:  # noqa: BLE001
            # One unreadable file must not lose the other 2,238.
            print(f"  ! skipped {path.name}: {exc}", file=sys.stderr)


def cap_dominant_category(rows: list[dict], max_per_category: int, seed: int = 42) -> list[dict]:
    """Even-sample any attack category that has swamped the rest.

    Measured need: 294,304 of 297,375 weblogs positives (99%) are
    `bruteforce_login_server_attempt` — near-identical 'Failed password for X
    from Y' lines from one server under sustained real attack. Left alone, the
    training set's positive class is in practice one template repeated 294k
    times, while sqli/xss/rce sit in single digits. Same failure shape the
    score cache guards against in production — one judgement applied to every
    repeat — solved the same way: cap, don't discard the pattern entirely.

    Only positives are touched. Grouping negatives the same way would put every
    normal line under one `alert_category=None` bucket and downsample all of
    them together, which is not the problem being fixed.
    """
    import random

    normals = [row for row in rows if not row["label"]]
    by_category: dict[str, list[dict]] = {}
    for row in rows:
        if row["label"]:
            by_category.setdefault(row["alert_category"], []).append(row)

    rng = random.Random(seed)
    capped: list[dict] = []
    for category, group in by_category.items():
        if len(group) <= max_per_category:
            capped.extend(group)
            continue
        rng.shuffle(group)
        capped.extend(group[:max_per_category])
    return normals + capped


def stratified_split(records: list[dict], seed: int = 42) -> dict[str, list[dict]]:
    """Seeded, class-balanced split, for a corpus with no single timeline.

    `chronological_split` is right for one stream of events in time order. winlog
    is not that: its negatives are one collection and its positives are another,
    concatenated. Splitting that by position put every Windows attack in the tail
    — measured at 25.56% anomalies in test against 0.61% in train, which is
    useless for training and meaningless for evaluation.

    There is no chronology to violate here, so shuffling is not leakage; it is the
    only way to get splits that are comparable to each other. nginx keeps the
    chronological split, because it genuinely is one site's traffic in time order.
    """
    import random

    rng = random.Random(seed)
    splits: dict[str, list[dict]] = {"train": [], "validation": [], "test": []}
    for label in (0, 1):
        rows = [record for record in records if record["label"] == label]
        rng.shuffle(rows)
        first, second = int(len(rows) * 0.70), int(len(rows) * 0.85)
        splits["train"].extend(rows[:first])
        splits["validation"].extend(rows[first:second])
        splits["test"].extend(rows[second:])
    return splits


# --- expert-labelled web/auth/syslog (Zenodo Web Server Log Dataset) --------

WEBLOGS_ROOT = RAW / "zenodo-weblogs"

# org -> files worth reading. Not every file in the archive: org-y ships ~150
# near-duplicate per-vhost access logs, and the ground-truth rules target the
# CyberPanel admin panel (`verifyLogin`, `websiteFunctions`), not the vhosts —
# reading all of them would be volume without matching signal.
WEBLOG_FILES = {
    "organization-x": [
        "log/apache2/web-access.log", "log/apache2/web-access.log.1",
        "log/auth.log", "log/auth.log.1",
        "log/syslog", "log/syslog.1",
    ],
    "organization-y": [
        "log/usr/local/lscp/cyberpanel/logs/access.log",
        "log/var/log/auth.log", "log/var/log/auth.log.1",
        "log/var/log/syslog", "log/var/log/syslog.1",
    ],
}

# A status code check, disguised as a substring match, is the exact trap fixed
# in services/collector/cache.py: a response byte count of 500-599 is not a
# status code. Anchored to where Apache's combined format actually puts one —
# right after the closing quote of the request line.
_STATUS_CODE_RULE = re.compile(r"^code:\s*(\d{3})$", re.IGNORECASE)


def load_ground_truth_rules(org: str) -> list[tuple[str, list[str]]]:
    """(label, AND-tokens) per rule, any rule matching is a positive.

    The dataset's own note says rules were authored to run against a
    log2timeline/plaso timeline, not raw log text — confirmed here: "code: 404"
    appears in the YAML and nowhere in the raw Apache log (grepped). That one
    field-reference form is translated below; every other token is the literal
    substring an analyst wrote after reading the actual attack, and does appear
    verbatim in the raw files (`Go-http-client`, `Failed password for`,
    `etc/passwd`, `verifyLogin`, a specific attacker or trusted IP).
    """
    import yaml

    path = WEBLOGS_ROOT / org / "ground-truth" / f"{org}.yaml"
    rules = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [(rule["ground_truth_label"], rule["filter"]) for rule in rules]


def _token_matches(token: str, line: str, line_lower: str) -> bool:
    status = _STATUS_CODE_RULE.match(token)
    if status:
        return f'" {status.group(1)} ' in line
    # Wildcard markers are SQL-LIKE style ("%Mozilla/5.0 ..."); a bare substring
    # search already ignores what they would have bounded, so stripping them is
    # the whole translation.
    return token.strip("%").lower() in line_lower


def classify_weblog(line: str, rules: list[tuple[str, list[str]]]) -> str | None:
    lowered = line.lower()
    for label, tokens in rules:
        if all(_token_matches(token, line, lowered) for token in tokens):
            return label
    return None


def read_weblog_file(path: Path, rules: list, stride: int, max_records: int) -> Iterator[dict]:
    import gzip

    if not path.exists():
        return
    opener = gzip.open if path.suffix == ".gz" else open
    kept = 0
    with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle):
            line = line.rstrip("\n").strip()
            if not line:
                continue
            category = classify_weblog(line, rules)
            if category is None and index % stride:
                continue
            message = _IPV4.sub("0.0.0.0", line)[:4000]  # a corrupt line can be pathological
            yield {
                "message": message,
                "label": 0 if category is None else 1,
                "alert_category": category,
                "corpus": "weblogs",
                "metadata": {},
            }
            kept += 1
            if kept >= max_records:
                return


def read_weblogs(stride: int, max_records: int) -> Iterator[dict]:
    for org, relative_paths in WEBLOG_FILES.items():
        rules = load_ground_truth_rules(org)
        for relative in relative_paths:
            yield from read_weblog_file(WEBLOGS_ROOT / org / relative, rules, stride, max_records)


# --- self-check -------------------------------------------------------------


def collections_count(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        if row["label"]:
            counts[row["alert_category"]] = counts.get(row["alert_category"], 0) + 1
    return counts


def self_check() -> None:
    """The labelling rules and the EVTX flattener, the two things that can be wrong."""
    attack = ('5.101.40.234 - - [23/Jan/2019:06:53:01 +0330] "GET /image/x?fhnz%3D1551%20AND%201%3D1'
              '%20UNION%20ALL%20SELECT%201 HTTP/1.1" 200 5667 "-" "Mozilla/5.0"')
    normal = ('31.56.96.51 - - [22/Jan/2019:03:56:16 +0330] "GET /image/61474/productModel/200x200 '
              'HTTP/1.1" 200 5379 "https://www.zanbil.ir/m/filter/b113" "Mozilla/5.0"')
    probe = '1.2.3.4 - - [22/Jan/2019:05:06:13 +0330] "GET /wp-login.php HTTP/1.1" 404 494 "-" "Mozilla"'
    # A visitor whose user-agent or referer merely mentions an attack word is not an attacker.
    innocent = '1.2.3.4 - - [22/Jan/2019] "GET /product/1 HTTP/1.1" 200 512 "http://x/wp-login.php" "UA"'

    assert classify_nginx(attack) == "sqli", classify_nginx(attack)
    assert classify_nginx(normal) is None, classify_nginx(normal)
    assert classify_nginx(probe) == "cms_probe", classify_nginx(probe)
    assert classify_nginx(innocent) is None, classify_nginx(innocent)

    # A 500-599 byte response is not an attack, and neither is a 404.
    assert classify_nginx('1.2.3.4 - - [x] "GET /a HTTP/1.1" 200 567 "-" "UA"') is None

    rendered = render_event({
        "System": {"EventID": 4625, "Channel": "Security", "Level": 0},
        "EventData": {"TargetUserName": "admin", "IpAddress": "10.0.0.5", "SubjectDomainName": "-"},
    })
    assert rendered.startswith("EventID=4625"), rendered
    # Identity is masked but the field survives: that an event names a user is
    # signal, which user it names is the corpus it came from.
    assert "TargetUserName=MASKED" in rendered, rendered
    assert "admin" not in rendered, f"account name leaked: {rendered}"
    assert "IpAddress=10.0.0.5" in rendered, rendered
    assert "Security" not in rendered and "Channel" not in rendered, f"channel leaked: {rendered}"
    assert "SubjectDomainName" not in rendered, f"empty '-' field kept: {rendered}"

    sid = render_event({"System": {"EventID": 4624},
                        "EventData": {"SubjectUserSid": "S-1-5-21-4230534742-2542757381-3142984815-1111",
                                      "ParentSid": "S-1-5-18"}})
    assert "S-1-5-21-MASKED" in sid and "4230534742" not in sid, sid
    assert "S-1-5-18" in sid, f"well-known SID should survive: {sid}"

    rules = [("dir_scan", ["code: 404", "MSIE 8.0"]), ("bruteforce", ["Failed password for"])]
    assert classify_weblog('1.2.3.4 - - [x] "GET /a HTTP/1.1" 404 512 "-" "Mozilla/4.0 (MSIE 8.0)"', rules) == "dir_scan"
    assert classify_weblog('1.2.3.4 - - [x] "GET /a HTTP/1.1" 200 404 "-" "Mozilla/4.0 (MSIE 8.0)"', rules) is None, \
        "a 404-byte response body must not be read as a 404 status"
    assert classify_weblog("Apr 27 sshd: Failed password for root from 1.2.3.4", rules) == "bruteforce"
    assert classify_weblog("Apr 27 sshd: Accepted publickey for deploy", rules) is None

    shared = shared_event_ids([
        {"message": "EventID=4688 x", "label": 0}, {"message": "EventID=4688 y", "label": 1},
        {"message": "EventID=9999 z", "label": 1},
    ])
    assert shared == {"4688"}, shared

    assert _IPV4.sub("0.0.0.0", "1.2.3.4 - - x") == "0.0.0.0 - - x"

    # The split bug this guards: positives arriving after negatives must not all
    # land in one bucket. Positional splitting of this input gives test 100%
    # anomalies and train 0%.
    rows = [{"label": 0, "message": str(i)} for i in range(100)]
    rows += [{"label": 1, "message": str(i)} for i in range(100)]
    parts = stratified_split(rows)
    rates = {name: sum(r["label"] for r in part) / len(part) for name, part in parts.items()}
    assert all(0.45 < rate < 0.55 for rate in rates.values()), rates
    assert sum(len(part) for part in parts.values()) == 200
    assert stratified_split(rows)["test"] == parts["test"], "split is not deterministic"

    dominant = (
        [{"label": 1, "alert_category": "bruteforce", "message": f"x{i}"} for i in range(1000)]
        + [{"label": 1, "alert_category": "sqli", "message": f"y{i}"} for i in range(5)]
        + [{"label": 0, "alert_category": None, "message": f"n{i}"} for i in range(2000)]
    )
    capped = cap_dominant_category(dominant, max_per_category=50)
    by_cat = collections_count(capped)
    assert by_cat.get("bruteforce", 0) == 50, by_cat
    assert by_cat.get("sqli", 0) == 5, "a category under the cap must be untouched"
    assert sum(1 for r in capped if not r["label"]) == 2000, "capping must not touch normals"

    print("self-check ok")


# --- main -------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", action="append", dest="corpora", choices=["nginx", "winlog", "weblogs"])
    parser.add_argument("--weblog-stride", type=int, default=32, help="keep every Nth normal weblogs line")
    parser.add_argument("--max-weblog-records", type=int, default=150_000, help="cap per weblogs file")
    parser.add_argument("--max-weblog-category", type=int, default=10_000,
                        help="cap any single weblogs attack category (measured need: ssh bruteforce alone)")
    parser.add_argument("--access-log", type=Path, default=RAW / "zanbil" / "access.log")
    parser.add_argument("--stride", type=int, default=64, help="keep every Nth normal nginx line")
    parser.add_argument("--max-records", type=int, default=400_000, help="cap per corpus")
    parser.add_argument("--max-per-evtx", type=int, default=400, help="cap per .evtx file")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--keep-disjoint-ids", action="store_true",
                        help="keep winlog EventIDs that occur in only one class (they are a free label)")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()

    if args.self_check:
        self_check()
        return 0
    self_check()

    selected = args.corpora or ["nginx", "winlog", "weblogs"]
    records: list[dict] = []
    sources: dict[str, dict] = {}

    if "nginx" in selected:
        if not args.access_log.exists():
            print(f"missing {args.access_log} — unzip data/access.log.zip into it", file=sys.stderr)
            return 1
        print(f"=== nginx ({args.access_log}) ===")
        rows = list(read_nginx(args.access_log, args.stride, args.max_records))
        records.extend(rows)
        sources["nginx"] = {**class_balance(rows), "stride": args.stride,
                            "licence": "CC0-1.0", "source": "doi:10.7910/DVN/3QBYB5"}
        print(f"  {len(rows):,} records, anomaly rate {class_balance(rows)['anomaly_rate']:.2%}")

    if "winlog" in selected:
        print("=== winlog ===")
        rows = list(read_evtx(RAW / "evtx-baseline", 0, "winlog", args.max_per_evtx))
        rows += list(read_evtx(RAW / "EVTX-to-MITRE-Attack", 1, "winlog", args.max_per_evtx))
        before = len(rows)
        if not args.keep_disjoint_ids:
            shared = shared_event_ids(rows)
            rows = [r for r in rows if (m := re.match(r"EventID=(\d+)", r["message"])) and m.group(1) in shared]
            print(f"  kept {len(rows):,} of {before:,} on {len(shared)} EventIDs present in both classes")
        rows = rows[: args.max_records]
        records.extend(rows)
        sources["winlog"] = {**class_balance(rows), "max_per_evtx": args.max_per_evtx,
                             "shared_event_ids_only": not args.keep_disjoint_ids,
                             "licence": "Apache-2.0 (benign) + CC0-1.0 (attacks)"}
        print(f"  {len(rows):,} records, anomaly rate {class_balance(rows)['anomaly_rate']:.2%}")

    if "weblogs" in selected:
        print("=== weblogs ===")
        if not WEBLOGS_ROOT.exists():
            print(f"missing {WEBLOGS_ROOT} — unzip organization-x.zip and organization-y.zip into it",
                  file=sys.stderr)
            return 1
        rows = list(read_weblogs(args.weblog_stride, args.max_weblog_records))
        before = len(rows)
        rows = cap_dominant_category(rows, args.max_weblog_category)
        if len(rows) != before:
            print(f"  capped dominant categories: {before:,} -> {len(rows):,}")
        records.extend(rows)
        sources["weblogs"] = {**class_balance(rows), "stride": args.weblog_stride,
                              "max_category": args.max_weblog_category,
                              "licence": "MIT (licence field unverified — Zenodo unreachable at build time)",
                              "source": "doi:10.5281/zenodo.20001206"}
        print(f"  {len(rows):,} records, anomaly rate {class_balance(rows)['anomaly_rate']:.2%}")

    if not records:
        print("No records produced.", file=sys.stderr)
        return 1

    # nginx is one site's traffic in time order, so the tail is held out.
    # winlog has no single timeline (see stratified_split), so it is stratified.
    # weblogs concatenates two organisations from non-overlapping time windows
    # (org-x Oct 2025, org-y Dec 2023-Apr 2025) — same shape as winlog, not one
    # timeline, so chronological splitting would strand one org in one split.
    splitters = {"nginx": chronological_split, "winlog": stratified_split, "weblogs": stratified_split}
    splits: dict[str, list[dict]] = {"train": [], "validation": [], "test": []}
    for corpus in sources:
        corpus_rows = [record for record in records if record["corpus"] == corpus]
        for name, chunk in splitters[corpus](corpus_rows).items():
            splits[name].extend(chunk)

    args.out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "sources": sources,
        "splits": {},
        "notes": {
            "nginx_labels": (
                "Rule-derived, not ground truth. Rules match the request path only — user-agent and "
                "referer are attacker-controlled, and matching them would flag any visitor who "
                "mentions wp-login. Categories: " + ", ".join(ATTACK_RULES)
            ),
            "nginx_base_rate": (
                "Every attack line is kept and normals are strided, so the anomaly rate in ALL splits "
                "including test is far above the ~0.02% seen in the wild. Precision measured on this "
                "test split is therefore optimistic. Real base rate has to come from a pilot."
            ),
            "nginx_privacy": "Client IPv4 addresses are masked to 0.0.0.0 before anything is written.",
            "nginx_test_positives": (
                "Web attacks arrive in bursts, so a chronological split spreads them unevenly: "
                "roughly 1047/117/45 across train/validation/test. Any nginx precision figure from "
                "the test split rests on ~45 positives and has a wide interval. Stratifying nginx "
                "would even it out at the cost of putting the same scanner session on both sides "
                "of the split, which is the worse trade."
            ),
            "winlog_render": (
                "One event becomes 'EventID=N field=value ...' from EventData only. Channel, Provider "
                "and Level are excluded: the attack samples come from channels the benign baseline "
                "barely populates, so including them teaches the source file, not the attack. Same "
                "shortcut as BGL's Level field."
            ),
            "winlog_shortcuts_removed": (
                "Measured on the first build of this corpus: EventID alone scored precision 0.998 "
                "(77 IDs occur only in attacks) and SubjectDomainName alone scored precision 1.000 "
                "(17 hostnames occur only in attacks) — the attack EVTX were captured on different "
                "machines than the benign baseline, so machine identity was the label. Host, domain, "
                "account names and domain-relative SIDs are now masked, and records are restricted to "
                "EventIDs present in both classes."
            ),
            "weblogs_licence_unverified": (
                "Web Server Log Dataset licence is recorded as MIT in ml/research/log-datasets.md from "
                "an earlier check, but Zenodo (doi:10.5281/zenodo.20001206) was unreachable when this "
                "corpus was built and the field was not re-confirmed. Verify before this reaches a "
                "shipped model."
            ),
            "weblogs_labels": (
                "Ground-truth YAML rules shipped with the dataset, written by analysts who investigated "
                "the real incidents. One field-reference form ('code: NNN', meant for a log2timeline "
                "timeline) is translated to a real HTTP status check; every other token is matched as a "
                "literal case-insensitive substring against the raw line."
            ),
            "leakage_warning": (
                "Both label sources are shortcuts a model can learn instead of the task. Read every "
                "metric against the keyword baseline in ml/eval/harness.py."
            ),
        },
    }

    for name, split_records in splits.items():
        path = args.out / f"{name}.jsonl"
        digest = write_jsonl(split_records, path)
        manifest["splits"][name] = {**class_balance(split_records), "sha256": digest, "path": path.name}
        print(f"  {name:<11} {len(split_records):>8,} records  anomalies "
              f"{class_balance(split_records)['anomaly_rate']:.2%}  sha256={digest[:16]}…")

    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8")
    ).hexdigest()
    (args.out / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\nmanifest: {args.out / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
