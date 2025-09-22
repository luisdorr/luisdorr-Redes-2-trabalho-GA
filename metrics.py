from __future__ import annotations

import logging
import re
import subprocess
from typing import Dict, Tuple, cast

# Valores default de banda (em Mbps) apenas como catálogo estático opcional.
STATIC_BANDWIDTH: Dict[Tuple[str, str], int] = {}

_LOGGER = logging.getLogger(__name__)


def _ordered_link(end_a: str, end_b: str) -> Tuple[str, str]:
    """Retorna uma tupla ordenada representando um link."""
    return cast(Tuple[str, str], tuple(sorted((end_a, end_b))))


def get_static_bandwidth(end_a: str, end_b: str) -> int | None:
    """Busca largura de banda estática se disponível."""
    return STATIC_BANDWIDTH.get(_ordered_link(end_a, end_b))


def measure_link_quality(
    neighbor_ip: str, count: int = 10, interval: float = 0.2
) -> Tuple[float, float, float]:
    """Mede latência, jitter e perda de pacotes para um vizinho.

    Retorna (avg_latency_ms, jitter_ms, packet_loss_percent).
    Em erro, retorna (inf, inf, 100).
    """

    cmd = [
        "env", "LANG=C",  # força saída em inglês
        "ping",
        "-c", str(count),
        "-i", str(interval),
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
        _LOGGER.error("Falha ao executar ping para %s: %s", neighbor_ip, exc)
        return float("inf"), float("inf"), 100.0

    output = completed.stdout + completed.stderr

    # 1. perda de pacotes
    loss_match = re.search(r"(\d+(?:\.\d+)?)% packet loss", output)

    # 2. RTT — suporta iputils ou busybox
    rtt_match = re.search(
        r"(?:=|:)\s*(?P<min>\d+\.\d+)/(?P<avg>\d+\.\d+)/(?P<max>\d+\.\d+)(?:/(?P<mdev>\d+\.\d+))?",
        output,
    )

    if not loss_match or not rtt_match:
        _LOGGER.warning("Não consegui parsear ping para %s. Saída:\n%s", neighbor_ip, output)
        return float("inf"), float("inf"), 100.0

    try:
        packet_loss = float(loss_match.group(1))
        avg_latency = float(rtt_match.group("avg"))
        # jitter pode não existir (busybox não tem mdev)
        jitter_str = rtt_match.group("mdev")
        jitter = float(jitter_str) if jitter_str else 0.0
    except Exception as exc:
        _LOGGER.error("Estatísticas de ping malformadas para %s: %s\nSaída:\n%s", neighbor_ip, exc, output)
        return float("inf"), float("inf"), 100.0

    return avg_latency, jitter, packet_loss


__all__ = [
    "STATIC_BANDWIDTH",
    "get_static_bandwidth",
    "measure_link_quality",
]
