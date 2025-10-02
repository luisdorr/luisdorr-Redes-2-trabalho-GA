#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"

usage() {
    cat <<USAGE
Usage: $SCRIPT_NAME <router_container_name|local> <iface|auto> <loss_percent> [<delay_ms>] [<jitter_ms>]

Examples:
  $SCRIPT_NAME r1 eth0 10 50 10
  $SCRIPT_NAME local auto 5

If <iface> is 'auto', the first non-loopback interface with an IPv4 address
inside the target container is selected.
USAGE
}

if [[ ${1:-} == "-h" || ${1:-} == "--help" ]]; then
    usage
    exit 0
fi

if [[ $# -lt 3 || $# -gt 5 ]]; then
    usage >&2
    exit 1
fi

ROUTER="$1"
IFACE="$2"
LOSS="$3"
DELAY="${4:-}"
JITTER="${5:-}"

validate_number() {
    local name="$1"
    local value="$2"
    local min="$3"
    local max="$4"

    if [[ -z "$value" ]]; then
        echo "Error: missing $name" >&2
        exit 1
    fi

    if ! [[ "$value" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
        echo "Error: $name must be a numeric value (got '$value')." >&2
        exit 1
    fi

    local float_value
    float_value=$(awk -v v="$value" 'BEGIN { printf "%0.4f", v }')

    local min_cmp max_cmp
    min_cmp=$(awk -v v="$float_value" -v m="$min" 'BEGIN { if (v < m) print 1; else print 0 }')
    max_cmp=$(awk -v v="$float_value" -v m="$max" 'BEGIN { if (v > m) print 1; else print 0 }')

    if [[ "$min_cmp" -eq 1 || "$max_cmp" -eq 1 ]]; then
        echo "Error: $name must be between $min and $max (got $value)." >&2
        exit 1
    fi
}

validate_integer() {
    local name="$1"
    local value="$2"

    if ! [[ "$value" =~ ^[0-9]+$ ]]; then
        echo "Error: $name must be an integer (got '$value')." >&2
        exit 1
    fi
}

validate_number "loss_percent" "$LOSS" 0 100
if [[ -n "$DELAY" ]]; then
    validate_integer "delay_ms" "$DELAY"
fi
if [[ -n "$JITTER" ]]; then
    if [[ -z "$DELAY" ]]; then
        echo "Error: jitter_ms requires delay_ms to be provided." >&2
        exit 1
    fi
    validate_integer "jitter_ms" "$JITTER"
fi

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

NETEM_ARGS="loss ${LOSS}%"
if [[ -n "$DELAY" ]]; then
    NETEM_ARGS+=" delay ${DELAY}ms"
    if [[ -n "$JITTER" ]]; then
        NETEM_ARGS+=" ${JITTER}ms distribution normal"
    fi
fi

echo "[INFO] Applying netem on $ROUTER: dev $IFACE $NETEM_ARGS"

HAS_NETEM=0
if run_in_target "tc qdisc show dev $IFACE | grep -q 'netem'"; then
    HAS_NETEM=1
fi

if [[ $HAS_NETEM -eq 1 ]]; then
    run_in_target "tc qdisc change dev $IFACE root netem $NETEM_ARGS"
    echo "[INFO] Updated existing netem qdisc on $IFACE."
else
    run_in_target "tc qdisc add dev $IFACE root netem $NETEM_ARGS"
    echo "[INFO] Added new netem qdisc on $IFACE."
fi

echo "[DONE] Netem configuration applied successfully."
