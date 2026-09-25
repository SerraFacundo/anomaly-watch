#!/bin/sh
# Inside the compose stack the detector is reachable at the service name, not
# localhost. --endpoint defaults to it here (Docker-level, not a replay.py
# change) so `docker compose run --rm replay <file> ...` works with no flags;
# an explicit --endpoint later in "$@" still wins (argparse keeps the last
# occurrence of a repeated option).
set -e
exec python tools/replay.py --endpoint "http://detector:8003" "$@"
