#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"

usage() {
    cat <<USAGE
Usage: $SCRIPT_NAME <router_container_name|local> <iface|auto>

Examples:
  $SCRIPT_NAME r1 eth0
  $SCRIPT_NAME local auto

If <iface> is 'auto', the first non-loopback interface with an IPv4 address
inside the target container is selected.
USAGE
}

if [[ ${1:-} == "-h" || ${1:-} == "--help" ]]; then
    usage
    exit 0
fi

if [[ $# -ne 2 ]]; then
    usage >&2
    exit 1
fi

ROUTER="$1"
IFACE="$2"

RUN_MODE=""
TARGET_HOSTNAME="$(hostname)"

if [[ "$ROUTER" == "local" || "$ROUTER" == "$TARGET_HOSTNAME" ]]; then
    RUN_MODE="local"
else
    if command -v docker >/dev/null 2>&1; then
        if docker inspect "$ROUTER" >/dev/null 2>&1; then
            RUN_MODE="docker"
        else
            echo "Error: container '$ROUTER' not found via docker." >&2
            exit 1
        fi
    else
        echo "Error: docker command not available and target '$ROUTER' is not local." >&2
        exit 1
    fi
fi

run_in_target() {
    local cmd="$1"
    if [[ "$RUN_MODE" == "docker" ]]; then
        docker exec "$ROUTER" bash -lc "$cmd"
    else
        bash -lc "$cmd"
    fi
}

if ! run_in_target "command -v tc >/dev/null 2>&1"; then
    echo "Error: 'tc' command not found inside target '$ROUTER'." >&2
    exit 1
fi

if [[ "$IFACE" == "auto" ]]; then
    IFACE="$(run_in_target "ip -o -4 addr show | awk '$2 != \"lo\" {print \$2}' | head -n1")"
    IFACE="${IFACE%% }"
    IFACE="${IFACE## }"
    if [[ -z "$IFACE" ]]; then
        IFACE="$(run_in_target "ip -o link show | awk '$2 !~ /lo:/ {gsub(\":\", \"\", \$2); print \$2}' | head -n1")"
    fi
    IFACE="${IFACE%% }"
    IFACE="${IFACE## }"
    if [[ -z "$IFACE" ]]; then
        echo "Error: unable to auto-detect interface inside '$ROUTER'." >&2
        exit 1
    fi
    echo "[INFO] Auto-detected interface: $IFACE"
fi

if ! run_in_target "ip link show $IFACE >/dev/null 2>&1"; then
    echo "Error: interface '$IFACE' not found inside '$ROUTER'." >&2
    exit 1
fi

echo "[INFO] Removing qdisc from $ROUTER: dev $IFACE"

if run_in_target "tc qdisc show dev $IFACE | grep -q '^qdisc'"; then
    if run_in_target "tc qdisc del dev $IFACE root"; then
        echo "[DONE] Removed qdisc from $IFACE."
    else
        echo "Error: failed to delete qdisc on $IFACE." >&2
        exit 1
    fi
else
    echo "[INFO] No qdisc to remove on $IFACE."
fi
