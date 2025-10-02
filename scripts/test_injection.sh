#!/usr/bin/env bash
set -euo pipefail

RESULTS_DIR="results"
mkdir -p "$RESULTS_DIR"

log() {
    echo "[$(date +'%Y-%m-%dT%H:%M:%S%z')] $*"
}

compose_up() {
    log "Starting docker-compose stack"
    docker-compose up -d
}

wait_for_containers() {
    local seconds="${1:-5}"
    log "Waiting ${seconds}s for containers to become ready"
    sleep "$seconds"
}

run_and_save() {
    local description="$1"
    local filename="$2"
    shift 2
    log "Running: $description"
    {
        echo "# $description"
        "${@}"
    } &>"$RESULTS_DIR/$filename"
}

if ! command -v docker >/dev/null 2>&1; then
    echo "Error: docker is required to run this test." >&2
    exit 1
fi

if ! command -v docker-compose >/dev/null 2>&1; then
    echo "Error: docker-compose is required to run this test." >&2
    exit 1
fi

compose_up
wait_for_containers 5

./scripts/inject_loss.sh r1 eth0 20 100 20
run_and_save "Ping with impairment" "ping_with_loss.txt" docker exec h1 ping -c 5 h2 || true

./scripts/recover_link.sh r1 eth0
run_and_save "Ping after recovery" "ping_after_recovery.txt" docker exec h1 ping -c 5 h2 || true

log "Test completed. Check the $RESULTS_DIR directory for outputs."
