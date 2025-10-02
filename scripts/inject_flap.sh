#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"

usage() {
    cat <<USAGE
Usage: $SCRIPT_NAME <router_container_name|local> <iface|auto> <down_seconds> <up_seconds> [<cycles>]

Simulates an interface flap by toggling the interface down/up for the given
number of cycles (default: 1).
USAGE
}

if [[ ${1:-} == "-h" || ${1:-} == "--help" ]]; then
    usage
    exit 0
fi

if [[ $# -lt 4 || $# -gt 5 ]]; then
    usage >&2
    exit 1
fi

ROUTER="$1"
IFACE="$2"
DOWN_TIME="$3"
UP_TIME="$4"
CYCLES="${5:-1}"

for value in "$DOWN_TIME" "$UP_TIME" "$CYCLES"; do
    if ! [[ "$value" =~ ^[0-9]+$ ]]; then
        echo "Error: timing parameters must be integers (seconds)." >&2
        exit 1
    fi
    if [[ "$value" -lt 0 ]]; then
        echo "Error: timing parameters must be non-negative." >&2
        exit 1
    fi
done

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

if [[ "$DOWN_TIME" -eq 0 && "$UP_TIME" -eq 0 ]]; then
    echo "[WARN] Both down and up times are zero; nothing to do." >&2
    exit 0
fi

echo "[INFO] Flapping $ROUTER:$IFACE for $CYCLES cycle(s) (down=${DOWN_TIME}s, up=${UP_TIME}s)"

for ((cycle = 1; cycle <= CYCLES; cycle++)); do
    echo "[INFO] Cycle $cycle/$CYCLES - interface down"
    run_in_target "ip link set dev $IFACE down"
    if [[ "$DOWN_TIME" -gt 0 ]]; then
        sleep "$DOWN_TIME"
    fi

    echo "[INFO] Cycle $cycle/$CYCLES - interface up"
    run_in_target "ip link set dev $IFACE up"
    if [[ "$UP_TIME" -gt 0 ]]; then
        sleep "$UP_TIME"
    fi

done

echo "[DONE] Interface flap simulation completed."
