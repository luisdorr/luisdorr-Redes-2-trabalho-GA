from __future__ import annotations

import logging
import re
import statistics
import subprocess
from typing import Dict, Tuple, cast

# Valores default de banda (em Mbps) apenas como catálogo estático opcional.
STATIC_BANDWIDTH: Dict[Tuple[str, str], int] = {}

_LOGGER = logging.getLogger(__name__)


def extract_rtt_samples(output: str) -> list[float]:
    """Extrai todas as amostras de RTT individuais do output do ping."""
    samples: list[float] = []
    for line in output.splitlines():
        time_match = re.search(r"time=(\d+(?:\.\d+)?)\s*ms", line)
        if time_match:
            samples.append(float(time_match.group(1)))
    return samples


def compute_jitter_from_samples(samples: list[float]) -> float:
    """Calcula jitter como desvio padrão populacional das amostras de RTT."""
    if len(samples) < 2:
        return 0.0
    return statistics.pstdev(samples)


def choose_jitter(
    samples: list[float],
    min_rtt: float | None,
    max_rtt: float | None,
    mdev_str: str | None,
) -> float:
    """
    Implementa política jitter-first com fallbacks:
    1. Se samples >= 2: usa desvio padrão das amostras
    2. Senão, se mdev disponível: usa mdev
    3. Senão, se min/max disponíveis: usa max-min
    4. Caso contrário: 0.0
    """
    if len(samples) >= 2:
        jitter = compute_jitter_from_samples(samples)
        _LOGGER.debug("Jitter calculado de %d amostras: %.3f ms", len(samples), jitter)
        return jitter
    
    if mdev_str:
        try:
            jitter = float(mdev_str)
            _LOGGER.debug("Jitter via mdev fallback: %.3f ms", jitter)
            return jitter
        except ValueError:
            pass
    
    if min_rtt is not None and max_rtt is not None:
        jitter = max(0.0, max_rtt - min_rtt)
        _LOGGER.debug("Jitter via max-min fallback: %.3f ms", jitter)
        return jitter
    
    _LOGGER.debug("Jitter fallback to 0.0 ms (insufficient data)")
    return 0.0


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

    Usa "jitter-first" approach: calcula jitter a partir das amostras individuais,
    com fallback para mdev (se disponível) ou max-min.
    
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
        min_rtt = float(rtt_match.group("min"))
        avg_latency = float(rtt_match.group("avg"))
        max_rtt = float(rtt_match.group("max"))
        mdev_str = rtt_match.group("mdev")

        # JITTER-FIRST: usar funções utilitárias para cálculo limpo
        rtt_samples = extract_rtt_samples(output)
        jitter = choose_jitter(rtt_samples, min_rtt, max_rtt, mdev_str)

    except Exception as exc:
        _LOGGER.error("Estatísticas de ping malformadas para %s: %s\nSaída:\n%s", neighbor_ip, exc, output)
        return float("inf"), float("inf"), 100.0

    return avg_latency, jitter, packet_loss


__all__ = [
    "STATIC_BANDWIDTH",
    "get_static_bandwidth",
    "measure_link_quality",
    "extract_rtt_samples",
    "compute_jitter_from_samples",
    "choose_jitter",
]
