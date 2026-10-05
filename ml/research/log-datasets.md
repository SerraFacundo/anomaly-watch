# Log datasets for a Linux + Windows fine-tune

## Why this note exists

`modernbert-ft` scores F1 0.930 on its BGL test split
(`ml/eval/results/modernbert-ft.json`). BGL is HPC supercomputer RAS logging from
LogHub, streamed via the `bolu61/loghub_2` mirror. On real systemd journal traffic
the same checkpoint:

- flags **21.5%** of routine lines as anomalies, **96%** of those from a single
  benign template;
- scores `Failed password for invalid user root` at **0.0000** (normal);
- scores `postgres FATAL: could not write to file` at **0.0000** (normal).

That is not a threshold problem. The model learned BGL's vocabulary — kernel,
midplane, RAS, torus — and nothing about sshd, nginx, systemd or Windows. The
0.930 measures fit to a supercomputer, and customers do not run supercomputers.

So the question is: what public log data exists that (a) is the domains customers
actually run, (b) carries **per-line** labels, because the detector is a per-line
binary classifier, and (c) can legally be used to train a model that is shipped to
paying self-hosting customers. All three conditions matter, and the honest headline
is that **almost nothing satisfies all three at once**. The rest of this note is the
evidence, and what to do given the constraint.

Every claim below is followed back to the dataset's own site, its Zenodo/DataCite
record, its LICENSE file, or the paper that publishes it. Where a licence could not
be reached from a primary source it says so, rather than guessing.

### Independent re-check, 2026-09-08

The load-bearing claims were re-fetched a second time, separately from the research
pass that produced this note, because a licence claim that turns out to be wrong is
worse here than no claim at all:

- **LogHub terms — confirmed.** `logpai/loghub` README, fetched directly:
  *"The datasets are freely available for research or academic work."* Commercial use
  is not mentioned anywhere in the terms.
- **`bolu61/loghub_2` — confirmed.** The Hugging Face API returns **no licence field
  and no licence tag**, and the card calls itself an *"Unofficial upload of LogHub 2.0
  dataset"*.
- **`NextronSystems/evtx-baseline` — confirmed.** GitHub API reports `Apache-2.0`.
  Release `v0.8.4` carries `all-evtx.tgz` at **621.2 MB**, plus per-platform archives
  (`win7-x86` 11.1 MB, `win10-client` 70.8 MB, `win11-client` 133.1 MB,
  `win11-client-2023` 169.5 MB, `win2022-ad` 66.0 MB, `win2022-0-20348-azure`
  143.2 MB). Note the GitHub repo *size* field reads 46 KB — the data lives in
  release assets, not in the tree.
- **Web Server Log Dataset (Zenodo 20001206) — NOT re-verified.** Zenodo returned
  `504 Gateway Time-out` on both `zenodo.org/api/records/20001206` and the
  `doi.org` resolver at the time of this check. The record's contents and its MIT
  rights field therefore rest on the original research pass alone. **Re-confirm the
  licence before any of this data reaches a training run** — it is recommendation #1
  below precisely because of that MIT field, so the recommendation falls if the field
  is not what it appeared to be.

## Summary

| Dataset | Log types | Labels | Size | Licence | Commercial OK? | Realism |
|---|---|---|---|---|---|---|
| [LogHub](https://github.com/logpai/loghub) (Linux, OpenSSH, Apache, Zookeeper, Proxifier, HealthApp, Mac, Windows) | Linux syslog, OpenSSH sshd, **Apache error only**, Zookeeper, Windows **CBS.log** | **None** on any of these | Linux 25.6k lines / 2.25 MiB; OpenSSH 655k / 70 MiB; Apache 56k / 4.9 MiB; Windows 114.6M / 26.09 GiB | Contradictory: Zenodo record says CC BY 4.0, its own licence text says "research or academic work" | **No — treat as research-only** | Real, but a single lab/university host each |
| [LogHub](https://github.com/logpai/loghub) (BGL, Thunderbird, HDFS, Hadoop, OpenStack) | HPC RAS, HDFS/Hadoop/OpenStack app logs | BGL/Thunderbird per-line; HDFS/Hadoop/OpenStack session-level | BGL 4.75M / 709 MiB | same as above | **No — treat as research-only** | Real production, wrong domain |
| [LogHub-2.0](https://github.com/logpai/loghub-2.0) | Same 14 systems | **Parsing templates only, not anomalies** | 52M+ lines | CC BY 4.0 on Zenodo, "research or academic" in text | **No** | as above |
| [`bolu61/loghub_2`](https://huggingface.co/datasets/bolu61/loghub_2) (HF, currently in use) | 15 LogHub corpora as raw text | Inline in the text for BGL only | 39.1M train rows, 5.05 GB | **None declared** — unofficial mirror | **No** | inherits LogHub |
| [AIT-LDS v2.1](https://doi.org/10.5281/zenodo.5789063) | Apache access + error, **auth.log**, audit, syslog, DNS, VPN, exim, horde, Suricata | **Per-line, by line number, with attack-step names** | 8 testbeds, 14–39 GB unpacked each | **CC BY-NC-SA 4.0** | **NO — NonCommercial** | Testbed, simulated users, real software |
| [AIT-LDS v1.1](https://doi.org/10.5281/zenodo.4264796) | Apache, exim, auth, Suricata (4 web servers) | Per-line | 6 days × 4 testbeds | **CC BY-NC-SA 4.0** | **NO — NonCommercial** | Testbed |
| [CSE-CIC-IDS2018](https://www.unb.ca/cic/datasets/ids-2018.html) | **Per-host Windows `.evtx`** + Ubuntu host log dirs, plus pcap and flow CSVs | Flow-level in CSVs; host logs **unlabelled**, only attack time windows | `logs.zip` 140–237 MB per day × 10 days (~400+ evtx per day); pcap ~38 GB/day | "You may redistribute, republish, and mirror … in any form" + citation | **Yes, with citation** | Testbed (AWS), 420 machines |
| [Web Server Log Dataset](https://doi.org/10.5281/zenodo.20001206) | **access.log, error.log, auth.log, syslog** (Apache + OpenLiteSpeed) | **Per-line, derivable** from published YAML rules | 60 + 208 log files; byte/line counts not stated in the record | **MIT** | **Yes** | **Real production**, two Indonesian public-sector orgs, real attacks |
| [Splunk BOTS v1/v2/v3](https://github.com/splunk/botsv3) | `access_combined`, `apache_error`, `linux_secure`, `linux_audit`, `syslog`, `wineventlog`, `xmlwineventlog:sysmon`, osquery, AWS, O365 | **None per-line** — CTF Q&A only | v3 320 MB pre-indexed | **CC0 1.0** | **Yes, unconditionally** | Mixed: "actual security incidents, or realistic lab recreations" |
| [mdecrevoisier/EVTX-to-MITRE-Attack](https://github.com/mdecrevoisier/EVTX-to-MITRE-Attack) | **EVTX**: Security, Sysmon, PowerShell, Defender, ADDS/ADCS, DNS, Firewall, Exchange, SQL | Per-**file** ATT&CK tactic/technique | >270 samples, ~3.3 MB repo | **CC0 1.0** | **Yes, unconditionally** | Lab attack simulation |
| [sbousseaden/EVTX-ATTACK-SAMPLES](https://github.com/sbousseaden/EVTX-ATTACK-SAMPLES) | **EVTX**: Windows + Sysmon channels | Per-**file** ATT&CK tactic/technique | ~200 EVTX, ~6.3 MB repo | **GPL-3.0** (`LICENSE.GPL`) | Yes, but copyleft — see below | Lab attack simulation |
| [OTRF/Security-Datasets](https://github.com/OTRF/Security-Datasets) | Windows Security/Sysmon/PowerShell as JSON + EVTX; some Linux, some AWS | Per-**dataset** ATT&CK mapping in YAML metadata | ~830 MB repo, 9 ATT&CK tactic folders for Windows | **MIT** (`LICENSE`) | **Yes** | Lab attack simulation |
| [NextronSystems/evtx-baseline](https://github.com/NextronSystems/evtx-baseline) | **Benign EVTX**: Security, System, Application, Sysmon, PowerShell, TaskScheduler, DNS-Client, LSA | All benign by construction — that *is* the label | `all-evtx.tgz` 621 MB across Win7/10/11/2022/AD/Azure | **Apache-2.0** | **Yes** | Lab VM, install + basic user interaction |
| [LANL Unified Host and Network](https://csr.lanl.gov/data/2017/) | Windows event IDs 4624/4625/4688/4768/… as **normalised JSON tuples** | **None** | 90 days, ~1.6B events across the LANL family | Copyright waived (CC0-equivalent) | **Yes** | **Real production** enterprise |
| [LANL Comprehensive Multi-Source](https://csr.lanl.gov/data/cyber1/) | Windows auth, process, DNS, netflow as **normalised tuples** | `redteam.txt` marks compromise events in `auth.txt` | 1,648,275,307 events; ~12 GB gz | Copyright waived | **Yes** | **Real production**, real red team |
| [DARPA OpTC](https://github.com/FiveDirections/OpTC-data) | eCAR JSON host telemetry (process/file/flow) from 1000 Win10 hosts | **PDF timeline only**, no per-event labels | 17.4B events, ~1 TB | "Distribution A: Approved for public release: distribution unlimited" | Yes | Testbed / pilot study |
| [CIC-IDS2017](https://www.unb.ca/cic/datasets/ids-2017.html) | pcap + CICFlowMeter CSVs | Flow-level | — | citation required, "publicly available for researchers" | unclear | Testbed |
| [ADFA-LD](https://research.unsw.edu.au/projects/adfa-ids-datasets) | **System call traces, not logs** | Per-trace | — | "academic research purposes"; commercial use prohibited | **NO** | Testbed |
| [CLUE-LDS](https://doi.org/10.5281/zenodo.7119953) | Cloud-storage app events (logins, file access) as JSON | None shipped | ~50M events, 14.5 GB, 5000+ users, 1910 days | **CC BY 4.0** | **Yes** | **Real users**, wrong domain |

## The datasets in detail

### LogHub (logpai) — where we are now, and why it is a liability

LogHub's own README states the terms: *"The datasets are freely available for
research or academic work. For any usage or distribution of the datasets, please
refer to the loghub repository URL … and cite the loghub paper where applicable."*
([README](https://github.com/logpai/loghub)). The same sentence is repeated in the
description of the Zenodo record ([DOI
10.5281/zenodo.8196385](https://doi.org/10.5281/zenodo.8196385)).

The Zenodo record's *rights* field, however, says `cc-by-4.0` — Creative Commons
Attribution 4.0, which permits commercial use. The two are in direct conflict, and
the GitHub repository carries no SPDX licence file at all (the GitHub API reports
`NOASSERTION` for `logpai/loghub` and `logpai/loghub-2.0`). Given a licence
statement written by the authors that says "research or academic work", and a
metadata field that may simply be a Zenodo deposit default, **the safe reading is
research-only**. This is the single biggest legal exposure in the project, because
the shipped default model was trained on it.

On content, the Linux-flavoured corpora are much less useful than their names
suggest:

- **Linux** — 25,567 lines, 2.25 MiB, 263.9 days. Small enough to be a rounding
  error, and **no labels**.
- **OpenSSH** — 655,146 lines, 70.02 MiB, 28.4 days. The right log type, no labels.
- **Apache** — 56,481 lines, 4.90 MiB. It is the **error log only**; there is no
  Apache/nginx *access* log in LogHub. No labels.
- **Zookeeper / Proxifier / HealthApp / Mac** — irrelevant to the target domains,
  no labels.
- **Windows** — 114.6M lines, 26.09 GiB, and it is **not the Windows Event Log**.
  The dataset README states it was *"collected by aggregating a number of logs from
  a lab computer running Windows 7. The original logs were located at
  `C:\Windows\Logs\CBS`"*
  ([Windows/README.md](https://github.com/logpai/loghub/blob/master/Windows/README.md)) —
  Component Based Servicing, i.e. Windows Update servicing traces. It contains no
  logons, no process creation, no security events. It is worthless for this project
  and its name is actively misleading.

Only BGL, Thunderbird, HDFS_v1/v3, Hadoop and OpenStack are labelled, and of those
only BGL and Thunderbird are per-line; HDFS/Hadoop/OpenStack labels are session- or
block-level, which `ml/README.md` already documents as unusable for the per-line
framing.

**LogHub-2.0** ([repo](https://github.com/logpai/loghub-2.0), [DOI
10.5281/zenodo.8275861](https://doi.org/10.5281/zenodo.8275861)) does not help: its
annotations are *log templates for parsing*, not anomaly labels. Same
"research or academic work" wording, same CC BY 4.0 rights field.

### `bolu61/loghub_2` (Hugging Face) — the mirror currently in `prepare.py`

`ml/data/build/manifest.json` pins `bolu61/loghub_2` at revision
`4a98d3eb…`. The HF API reports **no licence field and no licence tag** on that
repo, and the card describes itself as an *unofficial* upload of LogHub 2.0
([dataset page](https://huggingface.co/datasets/bolu61/loghub_2)). It ships 15
plain-text corpora (`data/apache.txt`, `bgl.txt`, `linux.txt`, `openssh.txt`,
`thunderbird.txt`, …) plus `holdout/zookeeper.txt`, 39.1M train rows / 5.05 GB.

An unofficial mirror cannot grant rights the original does not. Whatever LogHub's
terms are, they are what apply. The absence of a licence on the mirror is not
permission; it is the absence of permission.

### AIT Log Data Set v2.1 — the perfect fit that we cannot use

This is, on content alone, exactly what the project needs. Eight testbeds, each a
small enterprise network — mail server, file share, WordPress, VPN, firewall — with
simulated user behaviour for 4–6 days and a multi-step attack injected. Collected
logs include *"Apache access and error logs, authentication logs, DNS logs, VPN
logs, audit logs, Suricata logs, network traffic packet captures, horde logs, exim
logs, syslog, and system monitoring logs"*
([Zenodo record 5789063](https://doi.org/10.5281/zenodo.5789063)).

The labelling is per-line and explicit. The `labels/` tree mirrors `gather/`, and
each label file contains one JSON object per attack-related line:

```json
{"line": 1860, "labels": ["attacker_change_user", "escalate"], "rules": {"attacker_change_user": ["attacker.escalate.audit.su.login"]}}
```

referring to line 1860 of the corresponding `gather/…/audit.log`. Attack steps are
nmap/WPScan/dirb scans, webshell upload (CVE-2020-24186), John the Ripper password
cracking, privilege escalation, remote command execution, and DNSteal exfiltration.
Unpacked sizes run 14–39 GB per testbed, with `_no-pcaps.zip` variants.

**And the licence is CC BY-NC-SA 4.0.** DataCite records
`cc-by-nc-sa-4.0` for [v2.1](https://doi.org/10.5281/zenodo.5789063),
[v1.1](https://doi.org/10.5281/zenodo.4264796) and the [Kyoushi Log Data
Set](https://doi.org/10.5281/zenodo.5779411); the accompanying paper *Maintainable
Log Datasets for Evaluation of Intrusion Detection Systems*
([arXiv:2203.08580](https://arxiv.org/abs/2203.08580)) states the same licence.

NonCommercial rules this out for a product sold to small businesses. ShareAlike
would additionally be a problem if a trained model counts as an adapted work — an
unsettled question that is not worth being the test case for. It is also worth
noting AIT is **Linux only**: Debian and Ubuntu hosts. There are no Windows logs in
it.

The derived [AIT Alert Data Set](https://doi.org/10.5281/zenodo.8263181) *is* CC BY
4.0, but it contains IDS *alerts* (Suricata/Wazuh/AMiner JSON), not raw log lines,
so it does not substitute.

### CSE-CIC-IDS2018 — the only permissive source of real `.evtx` at scale

The CIC page states plainly: *"You may redistribute, republish, and mirror the
CSE-CIC-IDS2018 dataset in any form. However, any use or redistribution of the data
must include a citation to the CSE-CIC-IDS2018 dataset and a link to this page in
AWS"* ([unb.ca/cic/datasets/ids-2018](https://www.unb.ca/cic/datasets/ids-2018.html)).
That is an attribution-only condition — commercially usable.

It also, unlike CIC-IDS2017, ships host logs. The page describes *"event logs
(windows and Ubuntu event Logs) per machine"*. This was verified directly against
the bucket rather than taken on trust. Listing
`s3://cse-cic-ids2018/Original Network Traffic and Log data/` shows ten day
directories, each with `logs.zip` (140–237 MB) and `pcap.zip` (~38 GB). Reading the
ZIP central directory of `Friday-16-02-2018/logs.zip` over an HTTP range request
returns entries like:

```
logs/DESKTOP-AN3U28N-172.31.64.111.evtx
logs/EC2AMAZ-O4EL3NG-172.31.64.100.evtx
```

— 400+ per-host `.evtx` files per day — and `Friday-02-03-2018/logs.zip`
additionally contains Ubuntu host directories `logs/U172.31.69.7`,
`logs/U172.31.69.15`, and so on. Download is
`aws s3 sync --no-sign-request "s3://cse-cic-ids2018/" dest-dir`; no registration.

**The catch is the labels.** The published labels are per-flow, in the
`Processed Traffic Data for ML Algorithms/` CSVs. The host logs carry no labels at
all. To use the EVTX for supervised per-line training you would have to label by
attack time window — the attack schedule is published per day — which yields noisy
labels: every benign event inside an attack window inherits a positive label unless
you filter further. Realistic but not free.

Realism: an AWS testbed, 50 attacking machines against 420 victim machines in five
departments. Not production, but real Windows hosts producing real Windows events.

### Web Server Log Dataset (Zenodo 20001206) — small, real, MIT, labelled

The best licence-to-usefulness ratio found. Real-world logs from two Indonesian
public-sector organisations: Organization X (Apache, 60 files, 2025-08-12 to
2025-10-07, reconnaissance and exploitation attempts) and Organization Y
(OpenLiteSpeed/CyberPanel, 208 files, including a successful privilege escalation on
2025-04-29). Artifact types are *"access.log, error.log, auth.log, and syslog"*.

Ground truth is shipped as YAML rules: *"Each rule specifies an attack type label, a
sensitivity mode (moderate or strict), and one or more filter patterns … A log entry
is assigned the corresponding attack label if all filter patterns in a rule are
matched. Entries that do not match any rule are labeled as benign."*
([DOI 10.5281/zenodo.20001206](https://doi.org/10.5281/zenodo.20001206)). So
per-line labels are reproducible from raw files.

Licence is recorded as **MIT** in the record's rights field. Commercial use permitted.

Two honest caveats. First, the labels are regex-derived, so a model trained on them
learns partly to imitate the rule set — the same class of shortcut as the BGL
`Level` leakage already documented in `ml/README.md`, and it needs the same kind of
baseline to keep it visible. Second, the record states no byte or line counts; 268
log files could be anything from a few MB to a few GB, and this could not be
verified without downloading.

### Online Shopping Store — Web Server Logs (zanbil.ir) — obtained, CC0, unlabelled

Supplied locally as `data/access.log.zip` (265 MB compressed) and since measured, so
this entry is fact rather than survey.

**10,365,152 lines** of real nginx combined-format access log from `zanbil.ir`, an
Iranian e-commerce site, January–February 2019. 3.5 GB uncompressed.

Primary source is Harvard Dataverse: *"Online Shopping Store - Web Server Logs"*,
Farzin Zaker, York University
([doi:10.7910/DVN/3QBYB5](https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi:10.7910/DVN/3QBYB5)).
The Dataverse API reports the licence as **CC0 1.0** — public domain, commercial use
permitted — but the same record adds *"Restricted due to sensitive content. Please
contact author to obtain access."* The widely-mirrored Kaggle copy
([eliasdabbas/web-server-access-logs](https://www.kaggle.com/datasets/eliasdabbas/web-server-access-logs))
carries the CC0 label without that access restriction.

So the licence is permissive and the *access control* is not. Those are different
things and both are true. The restriction is easy to explain by looking at the data:
every line carries the real client IP of a real person browsing a shop. That is
personal data, and it is a good reason to mask client addresses before this is used
for training — the model has no use for them, Drain3 templatises them to `<*>`
anyway, and keeping them only creates a memorisation and privacy liability.

**No labels.** It is a raw access log. Value is as negatives, as realistic base rate,
and — most immediately — as a measuring stick.

What measuring it produced:

- **nginx is the best case for the score cache, not the worst.** 119 templates per
  200,000 lines, i.e. **0.6 per 1,000**, against 20.3 for systemd journal. Hit
  ceiling **99.94%**. This closes the `ARCHITECTURE.md` §9 worry that high-cardinality
  URLs would break the cache and force the ONNX escape hatch. They do not: Drain3
  collapses the URL into `<*>` and the surrounding format is rigidly uniform.
- **It found a real bug in the cache bypass rules.** The HTTP 5xx pattern matched any
  bare 500–599 number, so a *response size* of 567 bytes was read as a 5xx status.
  12.65% of nginx traffic bypassed the cache instead of ~0%. Fixed by anchoring to
  where a status code actually appears; measured again at **0.02% bypass, 99.83% hit
  rate, 102 inferences for 60,000 lines**.
- **It confirmed the domain-transfer failure is worse than the journal number
  suggested.** `modernbert-ft` flags **99.00%** of this traffic as anomalous. The
  largest single group is 16,917 ordinary product-page `GET`s scored **0.793**.

### Splunk BOTS v1/v2/v3 — CC0, right sourcetypes, no per-line labels

`splunk/botsv1`, `botsv2` and `botsv3` are all **CC0-1.0** per the GitHub API, and
the v3 README carries the full CC0 text: *"the author(s) have dedicated all
copyright and related and neighboring rights to this software to the public domain
worldwide."* That is the most permissive licence in this note.

The v3 sourcetype list is close to the target domain list: `access_combined`,
`apache_error`, `linux_secure`, `linux_audit`, `syslog`, `bash_history`, `dmesg`,
`cron`, `wineventlog`, `winhostmon`,
`xmlwineventlog:microsoft-windows-sysmon/operational`, plus osquery, Symantec, AWS
and O365 ([README](https://github.com/splunk/botsv3)).

Two obstacles. It is distributed as a **pre-indexed Splunk index** (320.1 MB tgz,
`https://botsdataset.s3.amazonaws.com/botsv3/botsv3_data_set.tgz`) so extracting raw
lines means standing up Splunk Enterprise and exporting — a real but bounded chore.
And there are **no labels**: BOTS is a CTF, and the ground truth is a question-and-
answer key that Splunk distributes by emailing `bots@splunk.com`, not per-line
annotations. Its realistic value here is as a source of **CC0 in-domain normals**,
not as a supervised anomaly set.

Realism is mixed and the README says so: *"evidence captured during actual computer
security incidents, or from realistic lab recreations of security incidents."*

### Windows EVTX attack corpora

This is the weakest area, and the reason is structural: nobody publishes large
labelled Windows Security event logs, because a real one is a full map of an
organisation's users, machines and network.

What does exist is small, curated, per-file-labelled attack samples:

- **[mdecrevoisier/EVTX-to-MITRE-Attack](https://github.com/mdecrevoisier/EVTX-to-MITRE-Attack)**
  — **CC0-1.0** per the GitHub API. >270 EVTX samples covering Security (4624,
  4625, 4688, 4720, …), Sysmon v11+, PowerShell (800/4103/4104), Windows Defender,
  ADDS and ADCS/PKI, DNS Server, Firewall, Exchange and SQL Server. Mapping is a
  table of tactic / technique / description / event IDs. **This is the best-licensed
  Windows attack corpus available** and should be the first one adopted.
- **[sbousseaden/EVTX-ATTACK-SAMPLES](https://github.com/sbousseaden/EVTX-ATTACK-SAMPLES)**
  — ~200 EVTX across Execution, Persistence, Privilege Escalation, Defense Evasion,
  Credential Access, Discovery, Lateral Movement, C2. Licence is **GPL-3.0**
  (`LICENSE.GPL`, confirmed via the GitHub licence API). GPL on a *dataset* is an
  awkward fit and the copyleft reach over a model trained on it is untested. Usable,
  but it introduces a question that CC0 alternatives do not. Prefer the CC0 corpus;
  add this only if more attack coverage is needed and record the decision.
- **[OTRF/Security-Datasets](https://github.com/OTRF/Security-Datasets)** — **MIT**
  (verified by reading `LICENSE`: "MIT License, Copyright (c) 2021 Open Threat
  Research Forge"). Note the repo README footer says GPL-3.0, contradicting both the
  badge and the actual LICENSE file; the LICENSE file is the operative text but this
  ambiguity should be recorded. ~830 MB, organised as
  `datasets/atomic/windows/<tactic>/{host,network}/` — e.g. `credential_access/host`
  contains `empire_mimikatz_logonpasswords.zip`,
  `cmd_dumping_ntds_dit_file_ntdsutil.zip`, `psh_lsass_memory_dump_comsvcs.zip` — plus
  `datasets/atomic/linux/` and `aws/`. Data is mostly JSON-converted events with YAML
  metadata carrying the ATT&CK mapping. Labels are per-dataset, not per-event: each
  archive is "this is technique X being executed", and separating the attack events
  from the surrounding host noise inside an archive is manual work.
- **[NextronSystems/evtx-baseline](https://github.com/NextronSystems/evtx-baseline)**
  — **Apache-2.0**. This is the *benign* counterpart and it matters more than it
  looks: `all-evtx.tgz` in release v0.8.4 is **621 MB** of goodware EVTX from Win7
  x86, Win10, Win11 (two vintages), Win2022, Win2022 AD and Win2022 Azure, with
  Sysmon, PowerShell, Application/Security/System, TaskScheduler, DNS-Client and LSA
  channels enabled per the documented setup script. Every attack corpus above is
  positives-only; this is where the negatives come from.
- **[Yamato-Security/hayabusa-sample-evtx](https://github.com/Yamato-Security/hayabusa-sample-evtx)**
  — appears relevant but the GitHub API reports **no licence** on the repository.
  No licence means no permission. **Excluded.**

Every one of these is **lab attack simulation**, not captured intrusions. Treat them
as a source of attack *vocabulary* — the shape of a mimikatz LSASS access event, of a
4625 spray, of an encoded PowerShell 4104 — rather than as a base-rate-realistic
sample.

### LANL datasets — permissive and real, but not text

Both LANL sets waive copyright: *"To the extent possible under law, Los Alamos
National Laboratory has waived all copyright and related or neighboring rights"*
([2017 set](https://csr.lanl.gov/data/2017/), [cyber1
set](https://csr.lanl.gov/data/cyber1/)). Real production data from the LANL
enterprise network. `cyber1` even ships red-team ground truth: `redteam.txt`
contains *"specific events taken from the authentication data that present known
redteam compromise events"* across 1,648,275,307 events, 12,425 users and 17,684
computers.

The problem is the format. The host data is Windows event IDs (4624, 4625, 4688,
4768, 4769, 1100, …) rendered as de-identified JSON/CSV **tuples**: timestamp,
EventID, user, domain, logon type, process name, status — with users, computers and
processes replaced by anonymised identifiers. There is no message text. A per-line
text classifier trained on `U1234@DOM1,C567,C89,Kerberos,Network,LogOn,Success`
learns nothing transferable to a real journal line. Useful later if the detector ever
grows a structured-event path; not useful now.

### DARPA OpTC — big, permissive, effectively unlabelled

1000 Windows 10 hosts, 17.4 billion events, ~1 TB of compressed JSON in eCAR format,
released under *"Distribution A: Approved for public release: distribution
unlimited"* ([OpTC-data README](https://github.com/FiveDirections/OpTC-data)).
Hosted on Google Drive, not GitHub.

Ground truth is `OpTCRedTeamGroundTruth.pdf` — a **red-team timeline document**, not
per-event labels. Turning that into per-line labels is a research project in itself,
and the data is telemetry JSON rather than log text anyway. Not worth it for this
phase.

### Rejected quickly, with reasons

- **CIC-IDS2017** — pcap plus CICFlowMeter CSVs with 80+ flow features. Labels are
  flow-level. **No host logs.** Wrong shape for a per-line text classifier
  ([source](https://www.unb.ca/cic/datasets/ids-2017.html)).
- **ADFA-LD / ADFA-WD** — system call traces, not log files. Terms are explicit:
  *"Free use of these datasets for academic research purposes is hereby granted in
  perpetuity"*, with commercial use prohibited
  ([UNSW](https://research.unsw.edu.au/projects/adfa-ids-datasets)).
  **Research-only. Do not use.**
- **Kyoushi Log Data Set** ([DOI 10.5281/zenodo.5779411](https://doi.org/10.5281/zenodo.5779411))
  — found later, while chasing the Zenodo outage, and it is the AIT trap a second
  time. Content is close to ideal: an AIT testbed simulating a small enterprise
  (mail, file share, WordPress, VPN, firewall), with Apache access and error logs,
  auth logs, audit, DNS, syslog, and **per-line labels** carrying line number, label
  and matching rule name. OpenAIRE reports the rights as **CC BY-NC-SA** — same
  NonCommercial term as AIT-LDS, same institute, same labelling framework. **Rejected
  for the same reason.** Recorded here so the next person who finds it does not have
  to re-derive the licence. Note also that only logs from affected servers are
  labelled, so even under a permissive licence it would be partially supervised.
- **CLUE-LDS** — CC BY 4.0, and genuinely real: ~50M events from 5000+ real users of
  a cloud storage service over 1910 days, 14.5 GB JSON
  ([DOI 10.5281/zenodo.7119953](https://doi.org/10.5281/zenodo.7119953)). But the
  events are application-level (logins, file accesses, link shares, config changes)
  and ship no anomaly labels. Wrong domain.
- **Hugging Face** — searching the HF datasets API for `evtx`, `windows event log`,
  `nginx logs` and `auth log ssh` returns **zero results**. `syslog` and `log
  anomaly` return only small synthetic or unlicensed uploads
  (`vulcansiem/synthetic-syslog-1B`, `synthlab0/linux-syslog-anomalies-fr`,
  `lee900624/loganomaly`, `ridwanakm/hdfs-log-anomaly-dataset-lite`), none with a
  verifiable provenance or licence. There is no drop-in HF replacement for
  `bolu61/loghub_2`; anything adopted here will have to be prepared locally and,
  given `HF_HUB_OFFLINE=1`, that is consistent with how the stack already works.

## Recommendation

Aim for a mixed corpus rather than one dataset, because no single dataset covers both
platforms with labels and a usable licence. Ordered by effort-to-value:

**1. Web Server Log Dataset (MIT) — do this first.** Real nginx-class access logs,
error logs, auth.log and syslog, from production, with reproducible per-line labels
and a licence that raises no questions. It directly attacks the two probes the model
currently fails (`Failed password for invalid user root` sits squarely in its
auth.log content). Small enough to prepare in an afternoon. Build the equivalent of
the `level-keyword` baseline against the YAML rules so that "the model learned the
regex" stays visible.

**2. NextronSystems/evtx-baseline (Apache-2.0) + mdecrevoisier/EVTX-to-MITRE-Attack
(CC0) — the Windows pair.** 621 MB of benign multi-version Windows EVTX for the
negative class, >270 ATT&CK-mapped attack EVTX for the positive class. Both
permissive, both small enough to handle. Rendering EVTX to the one-line text the
detector consumes is a real preprocessing decision that will need its own design
note — the natural choice is the rendered message plus selected fields, and it must
avoid the Windows equivalent of the BGL `Level` leak (do not feed the event Level or
the channel name if the attack samples are drawn from channels the benign baseline
does not populate; that would be a pure shortcut).

**3. CSE-CIC-IDS2018 host logs (redistributable + citation) — scale for Windows.**
Ten days of 400+ per-host `.evtx` plus Ubuntu host logs, with a published attack
schedule. Labels must be synthesised from attack time windows, so treat it as
weakly-supervised: good for volume and for realistic Windows base rates, not for
clean positives. Costs ~1.5 GB of `logs.zip` downloads if the pcaps are skipped.

**4. Splunk BOTS v1–v3 (CC0) — in-domain normals only.** The right sourcetypes
(`access_combined`, `linux_secure`, `linux_audit`, `wineventlog`, Sysmon) under the
most permissive licence available. Use for negatives and for calibrating false
positive rate on realistic mixed traffic. Requires a Splunk export step; do this only
after 1–3 if more negatives are needed.

**Do not use, despite how attractive they look:**

- **AIT-LDS v1.1/v2.x** is the single best-matched dataset in existence for this
  problem — per-line attack labels on Apache access/error, auth.log, audit and syslog
  from a simulated small enterprise, which is *literally the customer profile*. It is
  **CC BY-NC-SA 4.0 (NonCommercial)** and therefore unusable in a commercially
  deployed product. Not "probably fine", not "arguably fine for training". If the
  project ever wants it, that is a licensing conversation with AIT, not a technical
  decision.
- **ADFA-LD** is academic-use-only and commercial use is expressly prohibited.
- **LogHub, including via `bolu61/loghub_2`,** is described by its own authors as
  available for "research or academic work". See the risk section below.
- **hayabusa-sample-evtx** has no licence at all.

### Licensing consequences for a shipped product

The stack already refuses non-permissive *model* licences —
`ml/download_models.py` allows only apache-2.0/MIT, because weights are redistributed
to self-hosting customers. **The same discipline is not currently applied to
datasets, and the shipped default model was trained on data whose own licence text
says "research or academic work".**

Two things follow. First, `ml/data/prepare.py` should gain the dataset equivalent of
the model licence gate: an explicit, per-corpus licence field, recorded in
`manifest.json` alongside the existing hashes, with anything not on an allowlist
refused. The manifest already carries provenance for reproducibility; carrying it for
legal reasons is the same mechanism. Second, the corpus licence should propagate into
the model card and `models.yaml` notes, so a future reader can answer "what may we
ship this weight file under" without re-deriving this note.

### SSH honeypots — the obvious idea, and the format trap in it

Looked at after the first pass, because `auth.log` positives are the sharpest
remaining gap. Two concrete corpora exist:

- **A 4-month interactive SSH honeypot dataset**, July–November 2025, 145,425 events
  ([paper](https://pmc.ncbi.nlm.nih.gov/articles/PMC13224135/),
  data [DOI 10.5281/zenodo.19629700](https://doi.org/10.5281/zenodo.19629700)).
  **CC BY 4.0** — commercial use permitted. Fields are exactly what an attack needs:
  `event_type` (connect, login, disconnect, exec_command), `level`, timestamp, source
  IP, attempted credentials, raw command payloads, session UUID.
- **PANDAcap SSH Honeypot Dataset** ([Zenodo 3759652](https://zenodo.org/records/3759652)),
  three days of brute-force traces from February 2020.

**Neither ships `auth.log`.** They ship SQLite, CSV and Cowrie-style JSON in the
honeypot's own schema. That distinction is not cosmetic for this project: the
detector scores a text line, and the deployed collector reads sshd's text lines. A
model trained on honeypot CSV learns the honeypot's format.

The fields could be rendered into sshd-shaped lines
(`Failed password for invalid user <u> from <ip> port <p> ssh2`), and that is worth
doing — but it is **synthesis**, and it carries the same failure this whole note
exists because of. Every positive would come from one renderer with one template,
while real sshd emits a dozen variants (`Invalid user`, `Connection closed by
authenticating user`, `maximum authentication attempts exceeded`, PAM lines). The
model would learn the renderer. That is the BGL `Level` leak wearing a new hat.

**The honest alternative is to generate the corpus.** A throwaway VM with real
`sshd` and real `nginx`, a scanner pointed at it (`hydra`, `nikto`, `sqlmap`), and
the resulting `auth.log` and `access.log` captured. Real daemons emit real format
variety, the labels are exact because the attack window is known to the second, there
is no licence question because the data is ours, and it is repeatable when a new
attack class matters. It is what AIT did to build the dataset we cannot use. It costs
a day, not a research programme, and it is the only route to `auth.log` positives that
does not end in either a NonCommercial licence or a synthetic template.

## Gaps — what is not publicly available

1. **Labelled Windows Security event logs at production scale and base rate.** Every
   permissive EVTX corpus found is either lab attack samples (hundreds of files,
   per-file labels, positives only) or lab goodware (negatives only). Nothing exists
   with real enterprise Windows traffic, real proportions, and per-event labels — and
   nothing will, because such a log is a complete inventory of an organisation's
   users and machines. LANL is the closest, and it is de-identified into tuples with
   the text stripped out.

2. **nginx specifically.** Every public web-server corpus found is Apache, Apache
   error, or OpenLiteSpeed. nginx's access and error formats are close enough to
   Apache combined that transfer is plausible, but that is an assumption and it
   should be measured, not assumed.

3. **systemd journal.** This is the format the deployed collector actually reads, and
   there is no public labelled corpus of it whatsoever. LogHub's "Linux" is 25k lines
   of classic syslog from one host. The 21.5% false positive rate that prompted this
   note was measured on journal traffic that exists only locally.

4. **A commercially-licensed dataset with per-line attack labels on Linux service
   logs.** AIT is the only dataset that has this and it is NonCommercial. The Web
   Server Log Dataset is the only permissive substitute and it is two organisations
   and 268 files. This is the sharpest gap in the whole picture.

5. **Base rates.** Every labelled attack dataset is a testbed where an attack was
   deliberately injected, so anomaly rates are orders of magnitude above what a real
   small business sees. Any threshold calibrated on these will be wrong in
   production, and calibration will have to be done on customer traffic regardless of
   what the model was trained on.

**What has to come from a pilot customer:** unlabelled but real nginx access/error,
sshd auth, systemd journal and Windows Security/Sysmon traffic, over weeks, at true
base rate — used for (a) threshold calibration, (b) measuring the false positive rate
that actually matters commercially, and (c) hard-negative mining, feeding back the
benign templates the model currently flags. Public data can teach the model what
Linux and Windows logs *look like*; only customer data can tell it what *normal*
looks like at a given site. That argues for the deployment shape the collector
already implies — ship a general model, calibrate per site — rather than for chasing
a bigger public training set indefinitely.
