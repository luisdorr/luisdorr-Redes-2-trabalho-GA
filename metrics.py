from __future__ import annotations

import logging
import math
import re
import statistics
import subprocess
from dataclasses import dataclass
from typing import Optional, Tuple

_LOGGER = logging.getLogger(__name__)

PING_LOSS = re.compile(r"(\d+(?:\.\d+)?)% packet loss")
PING_RTT = re.compile(
    r"(?:=|:)\s*(?P<min>\d+(?:\.\d+)?)/(?P<avg>\d+(?:\.\d+)?)/(?P<max>\d+(?:\.\d+)?)(?:/(?P<mdev>\d+(?:\.\d+)?))?"
)
PING_SAMPLE = re.compile(r"time=(\d+(?:\.\d+)?)\s*ms")


@dataclass(slots=True)
class QoSMetrics:
    """Latency, jitter, loss, and bandwidth observed between Layer-3 neighbours."""

    latency_ms: float
    jitter_ms: float
    loss_percent: float
    bandwidth_mbps: Optional[float]


@dataclass(frozen=True, slots=True)
class MetricWeights:
    """Relative influence of each QoS dimension on the routing decision."""

    latency: float
    jitter: float
    loss: float
    bandwidth: float


@dataclass(frozen=True, slots=True)
class NormalizationBounds:
    """Reference bounds used to map raw QoS values into a unitless routing cost."""

    latency_ms: float
    jitter_ms: float
    loss_percent: float
    bandwidth_mbps: float


STATIC_BANDWIDTH: dict[Tuple[str, str], float] = {
    ("r1", "r2"): 1000.0,
    ("r1", "r3"): 900.0,
    ("r2", "r3"): 800.0,
    ("r2", "r4"): 700.0,
    ("r3", "r5"): 700.0,
    ("r4", "r8"): 650.0,
    ("r5", "r8"): 650.0,
    ("r4", "r6"): 500.0,
    ("r5", "r7"): 500.0,
}


def get_reference_bandwidth(router_a: str, router_b: str) -> Optional[float]:
    """Return the nominal capacity configured for a Layer-3 adjacency."""

    key = tuple(sorted((router_a, router_b)))
    return STATIC_BANDWIDTH.get(key)


def measure_link_quality(
    neighbor_ip: str,
    *,
    count: int = 10,
    interval: float = 0.2,
    bandwidth_hint: Optional[float] = None,
) -> QoSMetrics:
    """Probe a neighbour with ICMP echo to characterise the link."""

    cmd = [
        "env",
        "LANG=C",
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
    except OSError as exc:
        _LOGGER.error("ping %s failed: %s", neighbor_ip, exc)
        return QoSMetrics(latency_ms=math.inf, jitter_ms=math.inf, loss_percent=100.0, bandwidth_mbps=bandwidth_hint)

    output = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")

    loss_match = PING_LOSS.search(output)
    rtt_match = PING_RTT.search(output)
    if not loss_match or not rtt_match:
        _LOGGER.warning("could not parse ping output for %s", neighbor_ip)
        return QoSMetrics(latency_ms=math.inf, jitter_ms=math.inf, loss_percent=100.0, bandwidth_mbps=bandwidth_hint)

    packet_loss = float(loss_match.group(1))
    latency = float(rtt_match.group("avg"))
    min_rtt = float(rtt_match.group("min"))
    max_rtt = float(rtt_match.group("max"))
    mdev_text = rtt_match.group("mdev")

    samples = [float(match.group(1)) for match in PING_SAMPLE.finditer(output)]

    if len(samples) >= 2:
        jitter = statistics.pstdev(samples)
    elif mdev_text:
        jitter = float(mdev_text)
    else:
        jitter = max(0.0, max_rtt - min_rtt)

    if not math.isfinite(latency):
        latency = math.inf
    if not math.isfinite(jitter):
        jitter = math.inf

    return QoSMetrics(latency_ms=latency, jitter_ms=jitter, loss_percent=packet_loss, bandwidth_mbps=bandwidth_hint)


def compute_qos_cost(metrics: QoSMetrics, weights: MetricWeights, bounds: NormalizationBounds) -> float:
    """Translate QoS observations into a scalar used by the link-state algorithm."""

    weight_sum = weights.latency + weights.jitter + weights.loss + weights.bandwidth
    if weight_sum <= 0:
        return 100.0

    latency_term = min(metrics.latency_ms / bounds.latency_ms, 1.0) if math.isfinite(metrics.latency_ms) else 1.0
    jitter_term = min(metrics.jitter_ms / bounds.jitter_ms, 1.0) if math.isfinite(metrics.jitter_ms) else 1.0
    loss_term = min(metrics.loss_percent / bounds.loss_percent, 1.0)

    if metrics.bandwidth_mbps is None or metrics.bandwidth_mbps <= 0:
        bandwidth_term = 1.0
    else:
        bandwidth_term = 1.0 - min(metrics.bandwidth_mbps / bounds.bandwidth_mbps, 1.0)

    score = (
        weights.latency * latency_term
        + weights.jitter * jitter_term
        + weights.loss * loss_term
        + weights.bandwidth * bandwidth_term
    ) / weight_sum

    return round(score * 100.0, 3)


__all__ = [
    "QoSMetrics",
    "MetricWeights",
    "NormalizationBounds",
    "compute_qos_cost",
    "get_reference_bandwidth",
    "measure_link_quality",
]
