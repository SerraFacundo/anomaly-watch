# Agent installation

FluentBit runs on each monitored machine, outside the Compose stack, on its own
deploy lifecycle. It is a dumb forwarder: it reads log files and the journal,
stamps the wire envelope, and posts batches to `ingest-service`. It does no
parsing and holds no model, so nothing about detection changing requires
touching a single agent.

## Install on a host

On the central machine, mint a credential:

```bash
uv run python tools/agent_token.py issue --host web1
```

Render the config for that host:

```bash
uv run python tools/render_agent_config.py --host web1 --ingest-host watch.internal --token awa_... --out /tmp/web1-fluent-bit.conf
```

Then on the monitored machine, install FluentBit from your distribution, copy
`/tmp/web1-fluent-bit.conf` to `/etc/fluent-bit/fluent-bit.conf` (mode 0600 — it
holds the bearer token), copy the ingest CA certificate to
`/etc/fluent-bit/anomaly-watch-ca.crt`, install `anomaly-watch-agent.service`,
and start it:

```bash
sudo systemctl enable --now anomaly-watch-agent
```

Confirm the host is reporting:

```bash
uv run python tools/agent_token.py list
```

An agent showing `issued, never used` never reached ingest. The usual causes are
a certificate the host does not trust and a firewall on 443, in that order.

## What the buffer promises

`storage.total_limit_size 512M` in the config is the guarantee and the limit at
the same time:

- while the central stack is down, records accumulate on **this** host's disk
  and are forwarded when it returns
- while the pipeline is behind, `ingest-service` answers `429` and the same
  buffer holds the backlog
- once 512MB is reached the agent stops buffering rather than filling the disk
  of the machine it is monitoring

512MB is roughly 8 hours at 200 lines/s uncompressed. That number is an estimate
until the pilot measures the real per-host line rate — see change task 10.5.

## Adding a log source

Add an `[INPUT]` block to the template, not to a host. Every input needs
`storage.type filesystem`; without it that input's records live in memory and
die with the process, which quietly voids the buffering guarantee above for
that source only.

## Removing a host

Revoke its token. The agent will start receiving `401` and stop being able to
ship; uninstall the service at leisure.

```bash
uv run python tools/agent_token.py revoke --agent-id web1-a3f2c1
```
