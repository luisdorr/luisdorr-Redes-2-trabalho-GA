"""Utility functions for measuring OSPF-Gaming link metrics."""

from __future__ import annotations

import logging
import re
import subprocess
from typing import Dict, Tuple, cast

# Default bandwidth values (in Mbps) for statically modelled links. The keys
# represent the pair of nodes that share the link. The tuple is ordered to keep
# lookups consistent regardless of the direction of the request.
STATIC_BANDWIDTH: Dict[Tuple[str, str], int] = {
    ("r1", "r2"): 1000,
    ("r1", "r3"): 900,
    ("r2", "r3"): 850,
    ("r2", "r4"): 900,
    ("r3", "r5"): 800,
    ("r4", "r5"): 750,
    ("r4", "r6"): 700,
    ("r5", "r7"): 650,
    ("r6", "r7"): 700,
    ("r6", "r8"): 600,
    ("r7", "r8"): 950,
    ("r1", "h1"): 1000,
    ("r8", "h2"): 1000,
}

_LOGGER = logging.getLogger(__name__)


def _ordered_link(end_a: str, end_b: str) -> Tuple[str, str]:
    """Return a consistently ordered tuple representing a network link."""

    return cast(Tuple[str, str], tuple(sorted((end_a, end_b))))


def get_static_bandwidth(end_a: str, end_b: str) -> int | None:
    """Retrieve the configured static bandwidth for a link if available."""

    return STATIC_BANDWIDTH.get(_ordered_link(end_a, end_b))


def measure_link_quality(
    neighbor_ip: str, count: int = 10, interval: float = 0.2
) -> Tuple[float, float, float]:
    """Measure latency, jitter and packet loss to a neighbour.

    The function executes the system ``ping`` command and parses its summary to
    derive the QoS metrics expected by OSPF-Gaming.

    Parameters
    ----------
    neighbor_ip:
        IP address of the neighbour that should be measured.
    count:
        Number of ICMP echo requests that should be transmitted.
    interval:
        Delay in seconds between individual echo requests.

    Returns
    -------
    tuple
        A tuple in the form ``(avg_latency_ms, jitter_ms, packet_loss_percent)``.
        When the measurement fails the function returns fallback values of
        ``(float("inf"), float("inf"), 100.0)``.
    """

    cmd = [
        "ping",
        "-c",
        str(count),
        "-i",
        str(interval),
        neighbor_ip,
    ]

    try:
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:  # pragma: no cover - defensive guard
        _LOGGER.error("Failed to execute ping for %s: %s", neighbor_ip, exc)
        return float("inf"), float("inf"), 100.0

    output = completed.stdout + completed.stderr

    loss_match = re.search(r"(\d+(?:\.\d+)?)% packet loss", output)
    rtt_match = re.search(
        r"=\s*(?P<min>\d+\.\d+)/(?P<avg>\d+\.\d+)/(?P<max>\d+\.\d+)/(?P<mdev>\d+\.\d+)",
        output,
    )

    if not loss_match or not rtt_match:
        _LOGGER.warning("Unable to parse ping output for %s", neighbor_ip)
        return float("inf"), float("inf"), 100.0

    try:
        packet_loss = float(loss_match.group(1))
        avg_latency = float(rtt_match.group("avg"))
        jitter = float(rtt_match.group("mdev"))
    except (ValueError, IndexError) as exc:  # pragma: no cover - defensive guard
        _LOGGER.error("Malformed ping statistics for %s: %s", neighbor_ip, exc)
        return float("inf"), float("inf"), 100.0

    return avg_latency, jitter, packet_loss


__all__ = [
    "STATIC_BANDWIDTH",
    "get_static_bandwidth",
    "measure_link_quality",
]
