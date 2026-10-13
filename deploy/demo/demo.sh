#!/usr/bin/env bash
# Local multi-node demo, integrated with the team's web app.
#
#   deploy/demo/demo.sh up                          certificate, tokens, app, whole stack
#   deploy/demo/demo.sh watch                       refreshing view of what the pipeline found
#   deploy/demo/demo.sh attack web1 [ssh|web|panic] inject an attack on one node
#   deploy/demo/demo.sh warmup                      background problems on db1 and mail1
#   deploy/demo/demo.sh down                        stop both stacks, keep data and agent buffers
#   deploy/demo/demo.sh reset                       stop both stacks and delete their data
#
# COMPOSE overrides the compose command (default: podman compose).
# APP_DIR is the checkout of the web app (default: ../Anomaly-Watch, next to this repo).
set -euo pipefail
cd "$(dirname "$0")/../.."

COMPOSE=${COMPOSE:-podman compose}
DEMO="$COMPOSE -f docker-compose.yml -f deploy/demo/compose.demo.yml"
NODES="web1 web2 auth1 db1 mail1"
TOKENS=deploy/demo/tokens.env
APP_DIR=${APP_DIR:-../Anomaly-Watch}
APP_CREDS=deploy/demo/app-credentials.env
# Users, roles, rules and settings, but none of the app's fictitious nodes, logs,
# alerts or topology: everything on screen comes from this pipeline.
SEED_SKIP=MonitoredNodesSeeder,EventLogsSeeder,AlertsSeeder,NetworkTopologySeeder,CollectorsSeeder
CERTS=deploy/nginx/certs
DSN=postgresql://anomaly:anomaly@postgres:5432/anomaly_watch

if [ -f "$TOKENS" ]; then set -a; . "$TOKENS"; set +a; fi

# Its own project name: both checkouts lowercase to "anomaly-watch", and two
# compose projects with one name share containers, so a `down` of one would take
# the other with it.
app() { (cd "$APP_DIR" && $COMPOSE -p aw-app "$@"); }

app_up() {
  [ -d "$APP_DIR" ] || { echo "No app checkout at $APP_DIR; set APP_DIR" >&2; exit 1; }
  if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo "Created $APP_DIR/.env from .env.example"
  fi
  if grep -q '^DEPLOYMENT_MODE=Showcase' "$APP_DIR/.env"; then
    echo "DEPLOYMENT_MODE=Showcase turns the app's pipeline endpoint off; use Production" >&2; exit 1
  fi
  PIPELINE_SERVICE_TOKEN=$(sed -n 's/^PIPELINE_SERVICE_TOKEN=//p' "$APP_DIR/.env")
  [ -n "$PIPELINE_SERVICE_TOKEN" ] || { echo "PIPELINE_SERVICE_TOKEN is empty in $APP_DIR/.env" >&2; exit 1; }
  export PIPELINE_SERVICE_TOKEN

  # backup-before-migrate too: `up api` pulls it in through depends_on (api ->
  # migrate -> backup-before-migrate), and its image only exists if built here.
  app build api web backup-before-migrate
  app up -d db redis
  # Explicit steps instead of depends_on conditions, which podman-compose does not
  # always honour: schema first, then the seed, then the API that serves them.
  # Both are idempotent, so every `up` runs them.
  app run --rm --no-deps migrate
  app run --rm --no-deps -e Seed__Skip="$SEED_SKIP" api seed
  app up -d api web
  python3 tools/app_setup.py --api "${APP_API_HOST_URL:-http://localhost:8080}" --out "$APP_CREDS"
}

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
    #
    # `-v3`, not `fsaecr/modernbert-ft`: that repo exists but holds one file and
    # no model.safetensors, so this download reached the guard below instead of
    # the weights. v3 went to its own repo rather than a branch of that one.
    MODEL_REPO=${MODEL_REPO:-fsaecr/modernbert-ft-v3}
    # Pinned, for the reason ml/models.yaml gives at the top of the file: a
    # branch moves, and a moved branch means the weights you evaluated are not
    # the weights you deployed.
    MODEL_REVISION=${MODEL_REVISION:-e1d3703d90af061aae46c40d4d01425679a6e7b2}
    echo "Weights missing; downloading $MODEL_REPO into models/cache/$model/"
    uvx --from huggingface_hub hf download "$MODEL_REPO" --revision "$MODEL_REVISION" \
      --local-dir "models/cache/$model" \
      --exclude ".gitattributes" --exclude "checkpoints/*" --exclude "*.bin" --exclude "*.pt"
    [ -f "models/cache/$model/model.safetensors" ] || { echo "Download did not produce model.safetensors" >&2; exit 1; }
  fi

  missing=""
  for node in $NODES; do
    grep -q "^TOKEN_${node^^}=" "$TOKENS" 2>/dev/null || missing="$missing $node"
  done
  if [ -n "$missing" ]; then
    # Base compose only: the overlay refuses to load until these tokens exist.
    # Only the nodes without one, so adding a node keeps the others' tokens.
    $COMPOSE up -d postgres
    until $COMPOSE exec -T postgres pg_isready -U anomaly -d anomaly_watch >/dev/null 2>&1; do sleep 2; done
    tmp=$(mktemp)
    [ -f "$TOKENS" ] && cat "$TOKENS" > "$tmp"
    for node in $missing; do
      # `z` relabels the mount for SELinux. Without it an enforcing host denies
      # container_t reading user_home_t and python exits with Errno 13.
      token=$($COMPOSE run --rm -v "$PWD/tools:/app/tools:ro,z" ingest \
        python tools/agent_token.py --dsn "$DSN" issue --host "$node" | awk '/^token/ {print $3}')
      [ -n "$token" ] || { echo "Could not issue a token for $node" >&2; rm -f "$tmp"; exit 1; }
      echo "TOKEN_${node^^}=$token" >> "$tmp"
    done
    mv "$tmp" "$TOKENS"
    chmod 600 "$TOKENS"
    set -a; . "$TOKENS"; set +a
  fi

  app_up
  $DEMO up -d --build
  # Precalentamiento: real alerts on db1 and mail1 before anyone looks, through the
  # real pipeline. The agents read from the head of each file, so this can run
  # while they are still starting.
  $DEMO run --rm loggen python -u /loggen.py warmup
  echo "Up. App: http://localhost:${FRONTEND_PORT:-3000} (admin@anomalywatch.local / password123)"
  echo "Next: deploy/demo/demo.sh watch, or $COMPOSE -f docker-compose.yml -f deploy/demo/compose.demo.yml logs -f bridge"
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
  warmup) $DEMO run --rm loggen python -u /loggen.py warmup ;;
  down) $DEMO down; app down ;;
  # Only the demo's own app project (aw-app): an app stack run by hand under its
  # default name keeps its data.
  reset) $DEMO down -v; app down -v; rm -f "$TOKENS" "$APP_CREDS" ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
