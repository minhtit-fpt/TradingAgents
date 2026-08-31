#!/bin/sh
# Scheduler entry point for the dividend module's weekly run. Same shape as
# value-daily.sh and a separate file on purpose: the two jobs must be
# schedulable, and fail, independently.
set -eu

export PATH=/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin:/usr/bin:/bin
cd "$(dirname "$0")/.."

exec docker compose -f docker-compose.value.yml run --rm value-dividend-weekly "$@"
