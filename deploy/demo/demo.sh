#!/usr/bin/env bash
# Local multi-node demo.
#
#   deploy/demo/demo.sh up                    certificate, tokens, whole stack
#   deploy/demo/demo.sh watch                 refreshing view of what was found
#   deploy/demo/demo.sh attack web1 [ssh|web] inject an attack on one node
#   deploy/demo/demo.sh down                  stop, keep data and agent buffers
#   deploy/demo/demo.sh reset                 stop and delete everything
#
# COMPOSE overrides the compose command (default: podman compose).
set -euo pipefail
cd "$(dirname "$0")/../.."

COMPOSE=${COMPOSE:-podman compose}
DEMO="$COMPOSE -f docker-compose.yml -f deploy/demo/compose.demo.yml"
NODES="web1 web2 auth1"
TOKENS=deploy/demo/tokens.env
CERTS=deploy/nginx/certs
DSN=postgresql://anomaly:anomaly@postgres:5432/anomaly_watch

if [ -f "$TOKENS" ]; then set -a; . "$TOKENS"; set +a; fi

up() {
  if [ ! -f "$CERTS/server.crt" ]; then
    # Same certificate as deploy/nginx/README.md. Agents trust it via tls.ca_file.
    mkdir -p "$CERTS"
    openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
      -keyout "$CERTS/server.key" -out "$CERTS/server.crt" \
      -subj "/CN=anomaly-watch.local" \
      -addext "subjectAltName=DNS:anomaly-watch.local,IP:127.0.0.1"
  fi

  model=${DETECTOR_MODEL_KEY:-modernbert-ft-v3}
  if [ ! -f "models/cache/$model/model.safetensors" ]; then
    # Private Hub repo, so HF_TOKEN must be set (or `hf auth login` done once).
    # ponytail: tracks the repo's main branch; pin with --revision <sha> if it ever changes.
    MODEL_REPO=${MODEL_REPO:-fsaecr/modernbert-ft}
    echo "Weights missing; downloading $MODEL_REPO into models/cache/$model/"
    uvx --from huggingface_hub hf download "$MODEL_REPO" --local-dir "models/cache/$model" \
      --exclude ".gitattributes" --exclude "checkpoints/*" --exclude "*.bin" --exclude "*.pt"
    [ -f "models/cache/$model/model.safetensors" ] || { echo "Download did not produce model.safetensors" >&2; exit 1; }
  fi

  if [ ! -f "$TOKENS" ]; then
    # Base compose only: the overlay refuses to load until these tokens exist.
    $COMPOSE up -d postgres
    until $COMPOSE exec -T postgres pg_isready -U anomaly -d anomaly_watch >/dev/null 2>&1; do sleep 2; done
    tmp=$(mktemp)
    for node in $NODES; do
      token=$($COMPOSE run --rm -v "$PWD/tools:/app/tools:ro" ingest \
        python tools/agent_token.py --dsn "$DSN" issue --host "$node" | awk '/^token/ {print $3}')
      [ -n "$token" ] || { echo "Could not issue a token for $node" >&2; rm -f "$tmp"; exit 1; }
      echo "TOKEN_${node^^}=$token" >> "$tmp"
    done
    mv "$tmp" "$TOKENS"
    chmod 600 "$TOKENS"
    set -a; . "$TOKENS"; set +a
  fi

  $DEMO up -d --build
  echo "Up. Next: deploy/demo/demo.sh watch"
}

watch_() {
  while true; do
    clear
    $COMPOSE exec -T postgres psql -U anomaly -d anomaly_watch -P pager=off -q \
      -c "SELECT agent_id, host, date_trunc('second', now() - last_seen) AS last_seen_ago
            FROM agents WHERE revoked_at IS NULL ORDER BY host" \
      -c "SELECT host, sum(count) AS lines, sum(anomaly_count) AS anomalies
            FROM template_counts WHERE hour >= date_trunc('hour', now()) - interval '1 hour'
            GROUP BY host ORDER BY host" \
      -c "SELECT to_char(coalesce(last_seen, detected_at), 'HH24:MI:SS') AS seen, host, source,
                 round(score::numeric, 4) AS score, occurrences AS n, left(message, 90) AS message
            FROM anomalies ORDER BY coalesce(last_seen, detected_at) DESC LIMIT 15" || true
    sleep 3
  done
}

case "${1:-}" in
  up) up ;;
  watch) watch_ ;;
  attack) $DEMO run --rm loggen python -u /loggen.py attack --node "${2:?node}" --kind "${3:-ssh}" ;;
  down) $DEMO down ;;
  reset) $DEMO down -v; rm -f "$TOKENS" ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
