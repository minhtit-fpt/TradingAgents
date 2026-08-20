#!/bin/sh
# Cron entry point for the value module's daily run. Cron gets a minimal PATH
# and Docker Desktop puts its binary and its credential helper in two different
# places, so both are named here rather than inherited.
set -eu

export PATH=/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin:/usr/bin:/bin
cd "$(dirname "$0")/.."

exec docker compose -f docker-compose.value.yml run --rm value-daily "$@"
