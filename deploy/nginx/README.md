# TLS certificates for the agent endpoint

Nginx terminates TLS in front of `ingest-service` and expects a certificate and
key in `deploy/nginx/certs/`. Neither is in the repository, and neither should
be: a private key in git is a private key on every machine that ever cloned it.

## Self-signed, for a pilot on one internal host

```bash
mkdir -p deploy/nginx/certs
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout deploy/nginx/certs/server.key \
  -out deploy/nginx/certs/server.crt \
  -subj "/CN=anomaly-watch.local" \
  -addext "subjectAltName=DNS:anomaly-watch.local,IP:127.0.0.1"
```

Agents must then trust that certificate explicitly — FluentBit's `tls.ca_file`
pointing at `server.crt`. Do **not** reach for `tls.verify Off` to make a
connection work: it turns the TLS into decoration, since anything on the network
can then present its own certificate and collect the fleet's logs.

## A real deployment

Use the customer's internal CA, or Let's Encrypt if the ingest hostname resolves
publicly. The agent config changes by one path either way.

## Rotation

Certificate expiry is a fleet-wide outage with a countdown attached: every agent
stops shipping on the same day. Set a reminder before the expiry date, and check
`tools/agent_token.py list` afterwards — an agent whose `last_seen` stopped
moving is one that did not accept the new certificate.
